"""清理 agent 作用域知识库的历史孤儿数据（默认 dry-run，只报告不落库）。

## 为什么会有孤儿数据

按 agent 作用域删除私有知识库时，``DELETE /api/enterprise/knowledge-bases/{id}``
只做「软隐藏」：把 ``agent_knowledge_branches.status`` 与
``agent_resource_bindings.status`` 置为 ``deleted``，接口返回 ``{"status": "hidden"}``。
全库**没有任何 restore / undelete 端点**，前端也没有入口，于是留下两类脏数据：

* **A 类 · 孤儿行**：``agent_knowledge_branches`` / ``agent_skill_branches`` /
  ``agent_resource_bindings`` 里 ``agent_id`` 指向的 agent 已被删除 —— 这些行
  永远不可能被读到，纯粹是垃圾。（另有一种「agent 健在、但引用的 skill/kb 已删」
  的行，本脚本**只报告不清理**，因为 agent 可能仍在引用它。）
* **B 类 · 幽灵库**：``knowledge_bases`` 行仍是 ``active``，文档 / bucket / chunk
  全在，但 branch + binding 都是 ``deleted``，列表按 ``branch.status != 'deleted'``
  过滤 → 用户和**管理员**都看不到，只能靠脚本处理。

## 清理项与开关

| 项 | 数据 | 默认 | 开关 |
| --- | --- | --- | --- |
| A | 孤儿的 branch / binding 行（agent 已不存在） | 只报告 | ``--apply`` |
| B | 被隐藏的私有库（owner agent 健在） | 只报告 | ``--restore-hidden`` 恢复 / ``--purge-kbs`` 真删 |
| C | 幽灵库（owner agent 已不存在） | 只报告 | ``--purge-kbs`` 真删 |

真删严格对齐 ``delete_knowledge_base`` 的级联顺序，删完顺带失效路由缓存。

## 用法

    # 1) 只报告（默认）
    cd backend && .venv/bin/python scripts/cleanup_knowledge_orphans.py

    # 2) 清 A 类孤儿行（安全，永不碰知识库内容）
    .venv/bin/python scripts/cleanup_knowledge_orphans.py --apply

    # 3) 把被隐藏的私有库恢复成可见（不删任何内容）
    .venv/bin/python scripts/cleanup_knowledge_orphans.py --apply --restore-hidden

    # 4) 真删被隐藏 / 幽灵的私有库（级联清文档，不可逆）
    .venv/bin/python scripts/cleanup_knowledge_orphans.py --apply --purge-kbs
    .venv/bin/python scripts/cleanup_knowledge_orphans.py --apply --purge-kbs kb_xxx kb_yyy

``--purge-kbs`` 不带 id 时，删除报告里全部 B/C 类知识库；带 id 时只删指定的那几个。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.database import engine  # noqa: E402
from app.db.models import (  # noqa: E402
    AgentKnowledgeBranch,
    AgentResourceBinding,
    AgentSkillBranch,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    utc_now,
)
from app.knowledge.route_cache import invalidate_knowledge_base  # noqa: E402

# 真删知识库时的级联顺序 —— 必须与 app/api/knowledge_bases.py::delete_knowledge_base 一致，
# 否则会留下指向已删库的悬空子行。
CASCADE_MODELS = (
    KnowledgeDiscoverySuggestion,
    KnowledgeIngestJob,
    KnowledgeConcept,
    KnowledgeChunk,
    KnowledgeBucket,
    KnowledgeDocument,
    KnowledgeBaseVersion,
    AgentKnowledgeBranch,
)

# 孤儿行扫描：(表名, 模型, 父表名, 分支侧指向父实体的列)。
#
# 注意父键各表不一样，踩过坑：
#   * agent_knowledge_branches.knowledge_base_id → 真 ``knowledge_bases.id``；
#   * agent_skill_branches.skill_id 存的是 **slug**（如 ``expense_travel_reimbursement``），
#     真 id 在 ``source_skill_id``（如 ``skill_47993266379d4269``）—— 拿 ``skill_id``
#     去 join ``skills.id`` 会命中 0 行，把 22 条健康分支全误判成孤儿。
ORPHAN_SCANS: tuple[tuple[str, Any, str, str], ...] = (
    ("agent_knowledge_branches", AgentKnowledgeBranch, "knowledge_bases", "knowledge_base_id"),
    ("agent_skill_branches", AgentSkillBranch, "skills", "source_skill_id"),
)

def private_scope_sql(alias: str) -> str:
    """判定「agent 私有作用域」的 SQL 片段；别名必须显式传入，否则多表 join 下会有歧义。"""

    return (
        f"coalesce({alias}.metadata_json ->> 'visibility', "
        f"{alias}.metadata_json ->> 'scope') = 'agent_private'"
    )


def mask_url(url: str) -> str:
    """脱敏连接串里的口令，报告里只留主机/库名。"""

    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url or "")


def banner(target: str) -> None:
    print("=" * 78)
    print("知识库孤儿数据清理")
    print(f"  目标库：{target}")
    print("=" * 78)


def collect_orphan_rows(session: Session) -> dict[str, dict[str, list[tuple[str, str, str]]]]:
    """A 类：parent（agent / 父资源）已不存在的 branch / binding 行。

    分开两类返回：

    * ``agent_missing`` —— agent 已删，这些行永远读不到，可以安全清理；
    * ``parent_missing`` —— agent 还在但 skill / kb 已删，**不清理**，只提示人工确认
      （agent 可能仍在引用这个分支，删掉会改变它的行为）。
    """

    found: dict[str, dict[str, list[tuple[str, str, str]]]] = {}
    for table, _model, parent_table, parent_column in ORPHAN_SCANS:
        rows = session.execute(
            text(
                f"""
                select r.id, r.tenant_id, r.agent_id, r.{parent_column} as parent_id,
                       case when a.id is null then 1 else 0 end as agent_missing,
                       case when p.id is null then 1 else 0 end as parent_missing
                from {table} r
                left join agent_profiles a on a.id = r.agent_id
                left join {parent_table} p on p.id = r.{parent_column}
                where a.id is null or p.id is null
                order by r.updated_at desc
                """
            )
        ).all()
        buckets: dict[str, list[tuple[str, str, str]]] = {"agent_missing": [], "parent_missing": []}
        for row in rows:
            item = (row[0], row[2], row[3])
            if row[4]:
                buckets["agent_missing"].append(item)
            else:
                buckets["parent_missing"].append(item)
        found[table] = buckets

    bindings = session.execute(
        text(
            """
            select b.id, b.tenant_id, b.agent_id, b.resource_type, b.resource_id
            from agent_resource_bindings b
            left join agent_profiles a on a.id = b.agent_id
            where a.id is null
            order by b.updated_at desc
            """
        )
    ).all()
    found["agent_resource_bindings"] = {
        "agent_missing": [(row[0], row[2], f"{row[3]}:{row[4]}") for row in bindings],
        "parent_missing": [],
    }
    return found


def collect_private_kbs(session: Session) -> list[dict[str, Any]]:
    """B/C 类：agent 私有作用域的知识库，附带 branch / binding 状态与 owner agent 存活情况。"""

    rows = session.execute(
        text(
            f"""
            select k.id, k.tenant_id, k.name, k.status,
                   k.metadata_json ->> 'owner_agent_id' as owner_agent_id,
                   b.status as branch_status,
                   b.agent_id as branch_agent_id,
                   g.status as binding_status,
                   a.id as agent_exists
            from knowledge_bases k
            left join agent_knowledge_branches b on b.knowledge_base_id = k.id
            left join agent_resource_bindings g
                   on g.resource_id = k.id and g.resource_type = 'knowledge_base'
            left join agent_profiles a
                   on a.id = coalesce(k.metadata_json ->> 'owner_agent_id', b.agent_id)
            where {private_scope_sql("k")}
            order by k.created_at
            """
        )
    ).all()

    result: list[dict[str, Any]] = []
    for row in rows:
        owner_agent_id = row[4] or row[6]
        branch_status = row[5] or "missing"
        binding_status = row[7] or "missing"
        agent_exists = row[8] is not None
        if agent_exists:
            # owner agent 还在，但绑定被软删 → 用户在界面上永远找不回来
            if branch_status == "deleted" and binding_status == "deleted":
                kind = "hidden"
            else:
                kind = "ok"
        else:
            # owner agent 已删，库里再没人能看到它
            if branch_status == "deleted" and binding_status == "deleted":
                kind = "ghost"
            elif branch_status == "missing" and binding_status == "missing":
                kind = "ok"  # 从未绑定过任何 agent（理论上不该出现）
            else:
                kind = "ghost"
        result.append(
            {
                "id": row[0],
                "tenant_id": row[1],
                "name": row[2],
                "kb_status": row[3],
                "owner_agent_id": owner_agent_id,
                "agent_exists": agent_exists,
                "branch_status": branch_status,
                "binding_status": binding_status,
                "kind": kind,
            }
        )
    return result


def report(
    session: Session,
    orphans: dict[str, dict[str, list[tuple[str, str, str]]]],
    kbs: list[dict[str, Any]],
) -> None:
    print("\n【A 类】孤儿 branch / binding 行")
    purgeable = 0
    pending = 0
    for table, buckets in orphans.items():
        agent_missing = buckets["agent_missing"]
        parent_missing = buckets["parent_missing"]
        purgeable += len(agent_missing)
        pending += len(parent_missing)
        if not agent_missing and not parent_missing:
            print(f"  {table:<28} 无")
            continue
        print(
            f"  {table:<28} agent 已删除 {len(agent_missing)} 行"
            f" / 父资源已删除 {len(parent_missing)} 行"
        )
        for row_id, agent_id, parent_id in agent_missing:
            print(f"      [可清理]      {row_id}  agent={agent_id}  parent={parent_id}")
        for row_id, agent_id, parent_id in parent_missing:
            print(f"      [待人工确认]  {row_id}  agent={agent_id}（健在）  parent={parent_id}（已删）")
    print(f"  可安全清理合计 {purgeable} 行；另 {pending} 行需人工确认（agent 仍在引用）")

    hidden = [item for item in kbs if item["kind"] == "hidden"]
    ghosts = [item for item in kbs if item["kind"] == "ghost"]

    print("\n【B 类】被隐藏的私有库（owner agent 健在，branch + binding 都被软删）")
    if not hidden:
        print("  (无)")
    for item in hidden:
        print(
            f"  - {item['id']}  {item['name']}\n"
            f"      tenant={item['tenant_id']}  owner_agent={item['owner_agent_id']}（健在）  "
            f"branch={item['branch_status']} binding={item['binding_status']}  kb={item['kb_status']}"
        )

    print("\n【C 类】幽灵库（owner agent 已不存在）")
    if not ghosts:
        print("  (无)")
    for item in ghosts:
        print(
            f"  - {item['id']}  {item['name']}\n"
            f"      tenant={item['tenant_id']}  owner_agent={item['owner_agent_id']}（已删除）  "
            f"branch={item['branch_status']} binding={item['binding_status']}  kb={item['kb_status']}"
        )

    ok = [item for item in kbs if item["kind"] == "ok"]
    print(f"\n（另有 {len(ok)} 个私有库状态正常，未列入清理范围）")


def purge_orphan_rows(
    session: Session, orphans: dict[str, dict[str, list[tuple[str, str, str]]]]
) -> int:
    """删除 A 类孤儿行 —— 只删「agent 已删除」的那些（父资源缺失的行留给人工）。"""

    deleted = 0
    for table, buckets in orphans.items():
        rows = buckets["agent_missing"]
        if not rows:
            continue
        model = {
            "agent_knowledge_branches": AgentKnowledgeBranch,
            "agent_skill_branches": AgentSkillBranch,
            "agent_resource_bindings": AgentResourceBinding,
        }[table]
        for row_id, _agent_id, _parent_id in rows:
            instance = session.get(model, row_id)
            if instance is not None:
                session.delete(instance)
                deleted += 1
        print(f"  [A] {table}：已删除 {len(rows)} 行")
    if not deleted:
        print("  [A] 无孤儿行需要清理")
    return deleted


def restore_hidden_kbs(session: Session, kbs: list[dict[str, Any]]) -> int:
    """把 owner agent 健在、绑定被软删的私有库恢复成可见。只改状态，不碰内容。"""

    restored = 0
    for item in kbs:
        if item["kind"] != "hidden":
            continue
        kb_id = item["id"]
        agent_id = item["owner_agent_id"]
        if not agent_id:
            print(f"  [B] 跳过 {kb_id}：拿不到 owner_agent_id，无法判断归属")
            continue

        branch = session.execute(
            AgentKnowledgeBranch.__table__.select().where(
                AgentKnowledgeBranch.tenant_id == item["tenant_id"],
                AgentKnowledgeBranch.agent_id == agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb_id,
            )
        ).first()
        if branch is not None:
            row = session.get(AgentKnowledgeBranch, branch.id)
            row.status = "active"
            row.sync_state = "synced"
            row.updated_at = utc_now()
        else:
            session.add(
                AgentKnowledgeBranch(
                    tenant_id=item["tenant_id"],
                    agent_id=agent_id,
                    knowledge_base_id=kb_id,
                    status="active",
                )
            )

        binding = session.execute(
            AgentResourceBinding.__table__.select().where(
                AgentResourceBinding.tenant_id == item["tenant_id"],
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == kb_id,
            )
        ).first()
        if binding is not None:
            row = session.get(AgentResourceBinding, binding.id)
            row.status = "active"
            row.updated_at = utc_now()
        else:
            session.add(
                AgentResourceBinding(
                    tenant_id=item["tenant_id"],
                    agent_id=agent_id,
                    resource_type="knowledge_base",
                    resource_id=kb_id,
                    status="active",
                )
            )
        print(f"  [B] {kb_id}（{item['name']}）已恢复为可见，owner_agent={agent_id}")
        restored += 1
    return restored


def purge_knowledge_bases(session: Session, kbs: list[dict[str, Any]], targets: list[str]) -> list[str]:
    """真删私有库并级联清理子表（对齐 delete_knowledge_base 的顺序）。"""

    candidates = [item for item in kbs if item["kind"] in {"hidden", "ghost"}]
    if targets:
        wanted = set(targets)
        unknown = wanted - {item["id"] for item in candidates}
        for kb_id in sorted(unknown):
            # 允许显式指定任意私有库（例如状态正常但用户想彻底删掉）
            row = session.execute(
                text(
                    f"select k.id, k.tenant_id, k.name, k.metadata_json ->> 'owner_agent_id' "
                    f"from knowledge_bases k "
                    f"where k.id = :kb_id and {private_scope_sql('k')}"
                ),
                {"kb_id": kb_id},
            ).first()
            if row is None:
                print(f"  [C] 跳过 {kb_id}：不是 agent 私有知识库或不存在")
                continue
            candidates.append(
                {
                    "id": row[0],
                    "tenant_id": row[1],
                    "name": row[2],
                    "owner_agent_id": row[3],
                    "kind": "explicit",
                }
            )

    purged: list[str] = []
    for item in candidates:
        kb_id = item["id"]
        tenant_id = item["tenant_id"]
        row = session.get(KnowledgeBase, kb_id)
        if row is None:
            print(f"  [C] 跳过 {kb_id}：知识库记录已不存在")
            continue
        child_total = 0
        for model in CASCADE_MODELS:
            children = session.execute(
                model.__table__.select().where(
                    model.tenant_id == tenant_id,
                    model.knowledge_base_id == kb_id,
                )
            ).all()
            for child in children:
                instance = session.get(model, child.id)
                if instance is not None:
                    session.delete(instance)
                    child_total += 1
        bindings = session.execute(
            AgentResourceBinding.__table__.select().where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == kb_id,
            )
        ).all()
        for binding in bindings:
            instance = session.get(AgentResourceBinding, binding.id)
            if instance is not None:
                session.delete(instance)
        session.delete(row)
        print(f"  [C] {kb_id}（{item['name']}）已删除，级联子行 {child_total} 条")
        purged.append(kb_id)
    return purged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清理 agent 作用域知识库的历史孤儿数据（默认 dry-run）",
    )
    parser.add_argument("--apply", action="store_true", help="真正落库；不加则只报告")
    parser.add_argument(
        "--restore-hidden",
        action="store_true",
        help="把被隐藏的私有库（owner agent 健在）恢复为可见",
    )
    parser.add_argument(
        "--purge-kbs",
        nargs="*",
        default=None,
        metavar="KB_ID",
        help="真删被隐藏 / 幽灵的私有库；不带 id 表示删报告里的全部",
    )
    args = parser.parse_args()

    banner(mask_url(str(engine.url)))
    if not args.apply:
        print("模式：dry-run（只报告）。加 --apply 才会写库。\n")

    with Session(engine) as session:
        orphans = collect_orphan_rows(session)
        kbs = collect_private_kbs(session)
        report(session, orphans, kbs)

        if not args.apply:
            print(
                "\n提示：以上只是报告。执行清理请加 --apply；"
                "恢复被隐藏的库加 --restore-hidden；真删加 --purge-kbs。"
            )
            return 0

        print("\n" + "-" * 78)
        print("开始执行清理")
        print("-" * 78)

        purge_orphan_rows(session, orphans)

        touched_tenants: set[str] = set()
        if args.restore_hidden:
            restore_hidden_kbs(session, kbs)
        if args.purge_kbs is not None:
            purged = purge_knowledge_bases(session, kbs, args.purge_kbs)
            for item in kbs:
                if item["id"] in purged:
                    touched_tenants.add(item["tenant_id"])

        session.commit()

        for tenant_id in sorted(touched_tenants):
            invalidate_knowledge_base(tenant_id)
            print(f"  已失效租户 {tenant_id} 的知识路由缓存")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
