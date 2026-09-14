"""历史 SOP 边条件一次性编译（回填脚本）。

把 ``skills``（含各 agent 分支 ``agent_skill_branches``）里**所有**已有的
自然语言边条件，一次性编译成结构化契约 ``EdgeConditionSpec`` 并写入
``skill_edge_conditions`` 表。新技能由写路径自动异步编译（见
``app/skills/edge_condition_jobs``），本脚本只负责把**存量**补齐——这是
「编译一次、跑无数次」能立刻见效的前提：没回填的图运行时依旧回落 LLM。

用法
----
::

    # 1. 先看会发生什么（默认 dry-run，不写库；会真的调 LLM 以给出准确结果）
    .venv/bin/python scripts/compile_skill_edge_conditions.py

    # 2. 只跑预设直译、完全不碰 LLM（零成本、零风险，先看能覆盖多少）
    .venv/bin/python scripts/compile_skill_edge_conditions.py --no-llm

    # 3. 真正落库
    .venv/bin/python scripts/compile_skill_edge_conditions.py --apply

    # 4. 复核覆盖情况（只读）
    .venv/bin/python scripts/compile_skill_edge_conditions.py --verify

    # 缩小范围 / 走 rq 异步
    .venv/bin/python scripts/compile_skill_edge_conditions.py --apply --tenant tenant_demo --skill sop_xxx
    .venv/bin/python scripts/compile_skill_edge_conditions.py --apply --queue

参数
----
- ``--tenant`` / ``--skill`` / ``--limit``：范围裁剪（可组合）。
- ``--apply``：真正写库；不加则 dry-run。``--dry-run`` 是它的显式反面。
- ``--no-llm``：禁用 LLM，只做前端预设 / 无条件边的确定性直译。
- ``--force``：连已有编译结果的边一起重编（默认跳过已覆盖的）。
- ``--queue``：不本地执行，把每个技能丢给 rq 的 ``skill_compile`` 队列
  （适合大租户；需要 ``uv run rq-worker`` 在跑）。
- ``--verify``：不编译，只读表统计覆盖率（可与 ``--apply`` 一起用）。
- ``--json``：额外输出一份机器可读的汇总。

退出码：``--apply`` 后仍有 ``failed`` / ``pending`` 时返回 1，便于 CI / 发布
前卡口；``llm_judge`` 不算失败——那是「确实需要模型语义判断」的正常结果。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from app.db import engine
from app.db.models import AgentSkillBranch, Skill
from app.skills.edge_condition_compiler import (
    compile_skill_conditions,
    iter_edges,
    node_index,
    summarize_kinds,
)
from app.skills.edge_condition_jobs import (
    authoritative_contents,
    compile_model_config,
    run_edge_condition_compile,
    schedule_edge_condition_compile,
)
from app.skills.edge_condition_spec import KIND_LABELS, is_unconditional, spec_from_condition_text


@dataclass
class Target:
    tenant_id: str
    skill_id: str
    agent_id: str | None = None
    name: str = ""
    version: str = ""

    @property
    def label(self) -> str:
        scope = f"agent={self.agent_id}" if self.agent_id else "open-gallery"
        return f"{self.skill_id}（{self.name or '未命名'}｜{scope}）"


@dataclass
class Totals:
    skills: int = 0
    # 非无条件边（前端「自定义条件」/ 预设落库后的中文条件边）
    conditional_edges: int = 0
    # 无条件边（default / else / 空条件），运行时本来就能直判，不算编译收益
    unconditional_edges: int = 0
    deterministic_preset: int = 0
    compiled: int = 0
    llm_judge: int = 0
    failed: int = 0
    pending: int = 0
    llm_calls: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def add_kinds(self, stats: dict[str, int]) -> None:
        for key, value in stats.items():
            self.kinds[key] = self.kinds.get(key, 0) + value


# ---------------------------------------------------------------------------
# 目标枚举
# ---------------------------------------------------------------------------


def _collect_targets(
    session: Session,
    *,
    tenant: str | None,
    skill: str | None,
    limit: int | None,
) -> list[Target]:
    skills = list(session.exec(select(Skill)).all())
    known = {(row.tenant_id, row.skill_id) for row in skills}
    targets: list[Target] = []
    seen: set[tuple[str, str, str | None]] = set()

    def _accept(
        tenant_id: str,
        skill_id: str,
        agent_id: str | None,
        name: str,
        version: str,
        content: Any,
    ) -> None:
        if tenant and tenant_id != tenant:
            return
        if skill and skill_id != skill:
            return
        # 纯线性图（全是无条件边）没有可编译的东西，直接排除——否则
        # ``--limit`` 会被一堆空目标吃掉，汇总里的「技能数」也失真。
        if _conditional_edge_count(content) == 0:
            return
        key = (tenant_id, skill_id, agent_id)
        if key in seen:
            return
        seen.add(key)
        targets.append(Target(tenant_id, skill_id, agent_id, name, version))

    for row in skills:
        _accept(
            row.tenant_id,
            row.skill_id,
            None,
            row.name or "",
            row.version or "",
            row.content_json,
        )
    for branch in session.exec(select(AgentSkillBranch)).all():
        if str(branch.status or "") == "deleted":
            continue
        if (branch.tenant_id, branch.skill_id) not in known:
            continue
        _accept(
            branch.tenant_id,
            branch.skill_id,
            branch.agent_id,
            branch.skill_id,
            branch.head_version or "",
            branch.content_json,
        )

    if limit is not None and limit >= 0:
        targets = targets[:limit]
    return targets


def _conditional_edge_count(content: Any) -> int:
    if not isinstance(content, dict):
        return 0
    nodes = node_index(content)
    count = 0
    for edge in iter_edges(content):
        source_id = str(edge.get("source_node_id") or "").strip()
        next_id = str(edge.get("next_node_id") or "").strip()
        if not source_id or not next_id or source_id not in nodes:
            continue
        if is_unconditional(edge.get("condition")):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# 三种模式
# ---------------------------------------------------------------------------


def run_dry(
    targets: list[Target],
    *,
    allow_llm: bool,
    verbose: bool,
) -> Totals:
    totals = Totals()
    with Session(engine) as session:
        for target in targets:
            row = _skill_row(session, target.tenant_id, target.skill_id)
            if row is None:
                continue
            contents = authoritative_contents(session, row, target.agent_id)
            content = contents[-1] if contents else {}
            edge_count = _conditional_edge_count(content)
            if edge_count == 0:
                continue
            totals.skills += 1
            totals.conditional_edges += edge_count
            model_config = compile_model_config(session, target.tenant_id, target.agent_id) if allow_llm else None
            outcome = compile_skill_conditions(content, model_config=model_config, allow_llm=allow_llm)
            totals.llm_calls += outcome.llm_calls
            totals.add_kinds(outcome.stats())
            # 统计口径与类型分布对齐：无条件边（always）单独计数，不混进
            # 「结构化 / 待模型判断」的分子分母，否则覆盖率会被虚高。
            for item in outcome.edges:
                if item.kind == "always":
                    totals.unconditional_edges += 1
                elif item.status == "compiled":
                    totals.compiled += 1
                elif item.status == "llm_judge":
                    totals.llm_judge += 1
                elif item.status == "failed":
                    totals.failed += 1
                    totals.failures.append(f"{target.label}｜{item.review_line()}")
            preset_translatable = sum(
                1
                for edge in iter_edges(content)
                if spec_from_condition_text(edge.get("condition")) is not None
            )
            totals.deterministic_preset += preset_translatable
            totals.rows.append(
                {
                    "target": target.label,
                    "conditional_edges": edge_count,
                    "preset_translatable": preset_translatable,
                    "summary": summarize_kinds(outcome.edges),
                    "llm_calls": outcome.llm_calls,
                }
            )
            if verbose:
                print(
                    f"  · {target.label:48s} 条件边 {edge_count:3d}｜"
                    f"LLM 调用 {outcome.llm_calls}｜{summarize_kinds(outcome.edges)}"
                )
    return totals


def run_apply(targets: list[Target], *, allow_llm: bool, force: bool, verbose: bool) -> Totals:
    totals = Totals()
    for target in targets:
        summary = run_edge_condition_compile(
            target.tenant_id,
            target.skill_id,
            target.agent_id,
            force=force or not allow_llm,
        )
        status = str(summary.get("status") or "")
        if status == "missing":
            print(f"  ! 跳过（技能不存在）：{target.label}")
            continue
        if status == "skipped":
            if verbose:
                print(f"  - {target.label:48s} 已是最新（{summary.get('reason')}）")
            continue
        totals.skills += 1
        totals.compiled += int(summary.get("edges") or 0)
        totals.llm_calls += int(summary.get("llm_calls") or 0)
        stats = dict(summary.get("stats") or {})
        totals.add_kinds(stats)
        totals.llm_judge += int(stats.get("llm_judge", 0))
        totals.failed += int(summary.get("stats", {}).get("failed", 0))
        totals.rows.append({"target": target.label, **{k: v for k, v in summary.items() if k != "stats"}})
        print(
            f"  ✓ {target.label:48s} 写入 {summary.get('written')} 行｜"
            f"LLM 调用 {summary.get('llm_calls')}｜{_kinds_text(stats)}"
        )
    return totals


def run_queue(targets: list[Target]) -> Totals:
    totals = Totals()
    for target in targets:
        job_id = schedule_edge_condition_compile(
            target.tenant_id,
            target.skill_id,
            agent_id=target.agent_id,
        )
        totals.rows.append({"target": target.label, "job_id": job_id})
        print(f"  ↻ {target.label:48s} 已入队 job={job_id or '（入队失败/无可用队列）'}")
    return totals


def run_verify(targets: list[Target], *, verbose: bool) -> Totals:
    from app.skills.edge_condition_jobs import edge_condition_review

    totals = Totals()
    with Session(engine) as session:
        for target in targets:
            review = edge_condition_review(
                session,
                tenant_id=target.tenant_id,
                skill_id=target.skill_id,
                agent_id=target.agent_id,
            )
            if not review["total"]:
                continue
            totals.skills += 1
            totals.conditional_edges += int(review["total"])
            stats = dict(review["stats"])
            totals.add_kinds(stats)
            totals.compiled += int(stats.get("compiled", 0))
            totals.llm_judge += int(stats.get("llm_judge", 0))
            totals.failed += int(stats.get("failed", 0))
            totals.pending += int(review["pending"])
            totals.rows.append(
                {
                    "target": target.label,
                    "total": review["total"],
                    "pending": review["pending"],
                    "stats": stats,
                }
            )
            if verbose:
                print(
                    f"  · {target.label:48s} 条件边 {review['total']:3d}｜待编译 {review['pending']:3d}｜"
                    f"{_kinds_text(stats)}"
                )
            for item in review["conditions"]:
                if item["status"] in {"failed", "pending"}:
                    totals.failures.append(
                        f"{target.label}｜{item['source_node_id']} → {item['next_node_id']}"
                        f"｜{item['condition'][:40]}｜{item['status']}"
                    )
    return totals


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _skill_row(session: Session, tenant_id: str, skill_id: str) -> Skill | None:
    return session.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()


def _kinds_text(stats: dict[str, int]) -> str:
    if not stats:
        return "（无）"
    parts = [f"{KIND_LABELS.get(key, key)}×{value}" for key, value in sorted(stats.items())]
    return "，".join(parts)


def _print_totals(totals: Totals, mode: str, *, allow_llm: bool) -> None:
    print(f"\n{'=' * 72}\n{mode} 汇总\n{'=' * 72}")
    print(f"  技能数（含 agent 分支）      : {totals.skills}")
    if totals.conditional_edges:
        print(f"  条件边总数（非无条件）        : {totals.conditional_edges}")
    if totals.unconditional_edges:
        print(f"  其中无条件边（本就可直判）    : {totals.unconditional_edges}")
    if mode == "DRY-RUN":
        print(f"  预设文本可零成本直译的        : {totals.deterministic_preset}")
    print(f"  已结构化（零 LLM 可判定）    : {totals.compiled}")
    print(f"  仍需模型语义判断 llm_judge   : {totals.llm_judge}")
    if totals.failed:
        print(f"  编译失败 failed              : {totals.failed}")
    if totals.pending:
        print(f"  尚未编译 pending             : {totals.pending}")
    if totals.llm_calls:
        print(f"  LLM 调用次数                 : {totals.llm_calls}")
    if totals.kinds:
        print(f"  结构化类型分布               : {_kinds_text(totals.kinds)}")
    if totals.compiled or totals.llm_judge:
        total = totals.compiled + totals.llm_judge + totals.failed
        if total:
            print(f"  可离线求值占比               : {totals.compiled / total:.0%}（结构化 / 已处理）")
    if totals.failures:
        print(f"\n  需要跟进的 {len(totals.failures)} 条：")
        for line in totals.failures[:20]:
            print(f"    · {line}")
        if len(totals.failures) > 20:
            print(f"    …… 其余 {len(totals.failures) - 20} 条省略")
    if not allow_llm and mode == "DRY-RUN":
        print(
            "\n  提示：--no-llm 只统计确定性直译的覆盖；去掉它跑一次可以看到 LLM "
            "编译后真正的可离线求值比例。"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="历史 SOP 边条件一次性编译（回填）")
    parser.add_argument("--tenant", help="只处理指定租户")
    parser.add_argument("--skill", help="只处理指定 skill_id")
    parser.add_argument("--limit", type=int, help="最多处理多少个技能（含分支）")
    parser.add_argument("--apply", action="store_true", help="真正写入 skill_edge_conditions")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库（默认行为）")
    parser.add_argument("--no-llm", action="store_true", help="禁用 LLM，只做确定性直译")
    parser.add_argument("--force", action="store_true", help="连已有编译结果的边一起重编")
    parser.add_argument("--queue", action="store_true", help="交给 rq worker 异步执行")
    parser.add_argument("--verify", action="store_true", help="只读复核覆盖率")
    parser.add_argument("--json", action="store_true", help="额外输出机器可读汇总")
    args = parser.parse_args(argv)

    # allow_llm = not args.no_llm
    allow_llm = True
    # --verify 单独出现时只复核；与 --apply 同时出现则「先写后核」
    do_apply = bool(args.apply) and not args.dry_run
    verbose = True

    with Session(engine) as session:
        targets = _collect_targets(
            session,
            tenant=args.tenant,
            skill=args.skill,
            limit=args.limit,
        )

    print(f"扫描到 {len(targets)} 个技能/分支目标")
    if not targets:
        print("没有需要处理的目标。")
        return 0

    if args.queue and not args.verify:
        print("\n[QUEUE] 投递到 rq skill_compile 队列")
        totals = run_queue(targets)
        if args.json:
            print(json.dumps({"mode": "queue", "rows": totals.rows}, ensure_ascii=False, indent=2))
        return 0

    totals = Totals()
    if do_apply:
        mode = "APPLY"
        print(f"\n[APPLY] 写入 skill_edge_conditions（LLM {'启用' if allow_llm else '禁用'}）")
        totals = run_apply(targets, allow_llm=allow_llm, force=args.force, verbose=verbose)
        if args.verify:
            print("\n[VERIFY] 复核覆盖情况")
            verify_totals = run_verify(targets, verbose=verbose)
            verify_totals.llm_calls = totals.llm_calls
            totals = verify_totals
            mode = "APPLY + VERIFY"
    elif args.verify:
        mode = "VERIFY"
        print("\n[VERIFY] 只读复核")
        totals = run_verify(targets, verbose=verbose)
    else:
        mode = "DRY-RUN"
        print(f"\n[DRY-RUN] 只统计不写库（LLM {'启用' if allow_llm else '禁用'}）")
        totals = run_dry(targets, allow_llm=allow_llm, verbose=verbose)

    _print_totals(totals, mode, allow_llm=allow_llm)

    if args.json:
        print(
            "\n"
            + json.dumps(
                {
                    "mode": mode,
                    "totals": {
                        "skills": totals.skills,
                        "conditional_edges": totals.conditional_edges,
                        "compiled": totals.compiled,
                        "llm_judge": totals.llm_judge,
                        "failed": totals.failed,
                        "pending": totals.pending,
                        "llm_calls": totals.llm_calls,
                        "kinds": totals.kinds,
                    },
                    "rows": totals.rows,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    if mode.startswith("APPLY") and (totals.failed or totals.pending):
        print(f"\n⚠ 仍有 {totals.failed} 条编译失败 / {totals.pending} 条未编译，请跟进。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
