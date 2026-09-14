"""SOP 边条件离线编译的**异步执行层**。

编译要跑 LLM（整图一次批量调用，实测秒级到十几秒），绝不能挂在用户的保存 /
发布请求上。所以：

- 写路径（``api/skills.py`` 的 create / update / publish / 分支改写）只在
  真正有「未编译的条件边」时调一次 :func:`schedule_edge_condition_compile`，
  立即返回，不等结果；
- 实际执行走 +rq **独立 worker 进程**（``skill_compile`` 队列），Redis 不可用
  / ``SCHEDULER_BACKEND=poll`` 时自动降级到进程内异步队列
  （``app.async_jobs``，带 Redis 持久化与重启恢复）；
- 两条通道调用**同一个** ``run_edge_condition_compile``，语义完全一致。

幂等：编译前先比对指纹，图上所有条件边都已有非 ``failed`` 的编译结果就直接
返回 ``skipped``——重复入队（保存多次 / 重试 / 恢复）零成本。
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import AgentSkillBranch, Skill, SkillEdgeCondition
from app.skills.edge_condition_compiler import (
    CompiledEdge,
    compile_skill_conditions,
    iter_edges,
    node_index,
    node_required_fields,
    upsert_edge_conditions,
)
from app.skills.edge_condition_spec import condition_fingerprint, is_unconditional

logger = logging.getLogger(__name__)

JOB_NAME = "skill.edge_condition_compile"
# rq 的 import_attribute 只认**点分**路径（``pkg.mod.func``）。写成冒号形式
# （``pkg.mod:func``，pytest / entry-point 风格）入队阶段完全不报错，任务排进
# 队列后才在 worker 侧抛 ``ValueError: Invalid attribute name``——所以这里必须
# 是点分，并且由 ``tests/test_edge_condition_jobs.py`` 用 rq 自己的解析器守卫。
JOB_FUNC_PATH = "app.skills.edge_condition_jobs.run_edge_condition_compile"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def run_edge_condition_compile(
    tenant_id: str,
    skill_id: str,
    agent_id: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """编译一张 SOP 的全部条件边并落库（rq / 进程内异步队列的公共入口）。

    参数保持「字符串 + 可选字符串」——这是可跨进程序列化的最小契约。

    返回摘要（供观测/脚本打印）：``status`` ∈
    ``compiled`` / ``skipped`` / ``missing`` / ``error``。
    """

    with Session(engine) as db:
        row = db.exec(
            select(Skill).where(
                Skill.tenant_id == tenant_id,
                Skill.skill_id == skill_id,
            )
        ).first()
        if row is None:
            logger.warning("边条件编译跳过：技能不存在 tenant=%s skill=%s", tenant_id, skill_id)
            return {"status": "missing", "skill_id": skill_id}

        contents = authoritative_contents(db, row, agent_id)
        if not contents:
            return {"status": "skipped", "skill_id": skill_id, "reason": "no_content"}

        pending_contents = contents if force else [
            content for content in contents if has_uncompiled_edge(db, tenant_id, skill_id, content)
        ]
        if not pending_contents:
            return {"status": "skipped", "skill_id": skill_id, "reason": "up_to_date"}

        model_config = compile_model_config(db, tenant_id, agent_id)
        compiled_all: list[CompiledEdge] = []
        llm_calls = 0
        for content in pending_contents:
            outcome = compile_skill_conditions(content, model_config=model_config)
            llm_calls += outcome.llm_calls
            compiled_all.extend(outcome.edges)

        if not compiled_all:
            return {"status": "skipped", "skill_id": skill_id, "reason": "no_conditional_edge"}

        # 清理范围：整张图（含各 agent 分支）在用的指纹并集——只按当前这次
        # 编译的内容剪枝会把另一个分支的编译结果误删。
        # 剪枝只做一次，且以「整张图（含各分支）在用的指纹并集」为准：
        # upsert 自带的 prune 只看本次编译集合，会把已是最新（跳过编译）的那部分
        # 内容对应的行误删。
        written = upsert_edge_conditions(
            db,
            tenant_id,
            skill_id,
            row.version,
            compiled_all,
            prune_stale=False,
        )
        prune_outside(db, tenant_id, skill_id, in_use_fingerprints(contents))
        db.commit()

        summary = {
            "status": "compiled",
            "skill_id": skill_id,
            "version": row.version,
            "edges": len(compiled_all),
            "written": written,
            "llm_calls": llm_calls,
            "stats": kind_stats(compiled_all),
            "compiled_by": "script" if agent_id is None else "api",
        }
        logger.info(
            "SOP 边条件编译完成 tenant=%s skill=%s edges=%s llm_calls=%s",
            tenant_id,
            skill_id,
            summary["edges"],
            llm_calls,
        )
        return summary


def compile_model_config(db: Session, tenant_id: str, agent_id: str | None) -> Any:
    """编译用模型：意图识别轻量模型优先，回退租户默认模型。

    编译是**离线批处理**，不需要主模型级别的能力，但必须与运行时判定口径一致，
    所以走和 harness 同一套 ``model_for_agent`` 解析（agent 未指定时取租户默认）。
    """

    from app.agents.branching import model_for_agent  # 局部导入：避开 skills ↔ agents 环

    return model_for_agent(db, tenant_id, agent_id, "intent_recognition") or model_for_agent(
        db, tenant_id, agent_id
    )


def authoritative_contents(
    db: Session,
    row: Skill,
    agent_id: str | None,
) -> list[dict[str, Any]]:
    """返回需要编译的图内容列表：基础内容 + （指定 agent 时的）分支内容。

    运行时用的是「分支投影后」的技能内容，所以分支改写过图时必须编译分支内容；
    两类内容按条件指纹去重，同一条边只编译一次。
    """

    contents: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(content: Any) -> None:
        if not isinstance(content, dict):
            return
        key = content_fingerprint(content)
        if key in seen:
            return
        seen.add(key)
        contents.append(content)

    _add(row.content_json)
    if agent_id:
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == row.tenant_id,
                AgentSkillBranch.agent_id == agent_id,
                AgentSkillBranch.skill_id == row.skill_id,
            )
        ).first()
        if branch is not None and str(branch.status or "") != "deleted":
            _add(branch.content_json)
    return contents


def content_fingerprint(content: dict[str, Any]) -> str:
    material = json.dumps(
        [
            {
                "source_node_id": edge.get("source_node_id"),
                "next_node_id": edge.get("next_node_id"),
                "condition": edge.get("condition"),
            }
            for edge in iter_edges(content)
        ],
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def edge_fingerprints(content: dict[str, Any]) -> set[str]:
    nodes = node_index(content)
    fingerprints: set[str] = set()
    for edge in iter_edges(content):
        source_id = str(edge.get("source_node_id") or "").strip()
        next_id = str(edge.get("next_node_id") or "").strip()
        if not source_id or not next_id:
            continue
        required_fields = node_required_fields(nodes.get(source_id))
        fingerprints.add(
            condition_fingerprint(
                source_id,
                next_id,
                edge.get("condition"),
                required_fields,
            )
        )
    return fingerprints


def in_use_fingerprints(contents: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for content in contents:
        result |= edge_fingerprints(content)
    return result


def has_uncompiled_edge(
    db: Session,
    tenant_id: str,
    skill_id: str,
    content: dict[str, Any],
) -> bool:
    """是否还有「没有可用编译结果」的条件边（``failed`` 视为待重编）。"""

    needed = edge_fingerprints(content)
    if not needed:
        return False
    rows = db.exec(
        select(SkillEdgeCondition).where(
            SkillEdgeCondition.tenant_id == tenant_id,
            SkillEdgeCondition.skill_id == skill_id,
            SkillEdgeCondition.condition_fingerprint.in_(sorted(needed)),
        )
    ).all()
    covered = {
        row.condition_fingerprint
        for row in rows
        if str(row.status or "") != "failed"
    }
    return bool(needed - covered)


def prune_outside(
    db: Session,
    tenant_id: str,
    skill_id: str,
    in_use: set[str],
) -> None:
    rows = db.exec(
        select(SkillEdgeCondition).where(
            SkillEdgeCondition.tenant_id == tenant_id,
            SkillEdgeCondition.skill_id == skill_id,
        )
    ).all()
    for row in rows:
        if row.condition_fingerprint not in in_use:
            db.delete(row)


def kind_stats(edges: list[CompiledEdge]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in edges:
        key = item.kind or item.status
        counts[key] = counts.get(key, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------


def schedule_edge_condition_compile(
    tenant_id: str,
    skill_id: str,
    *,
    agent_id: str | None = None,
) -> str | None:
    """异步调度一次边条件编译；返回任务 id（不可用时返回 ``None``）。

    调用方（skills 写路径）**不等待结果**。rq 不可用时降级到进程内异步队列，
    因此本函数在两种部署形态下都返回非空 id（除非两者都不可用）。
    """

    func_path = JOB_FUNC_PATH
    try:
        from app.scheduled_tasks import rq_dispatch

        settings = get_settings()
        job_id = rq_dispatch.enqueue_job(
            settings.skill_compile_queue,
            func_path,
            tenant_id,
            skill_id,
            agent_id,
            timeout_seconds=settings.skill_compile_job_timeout_seconds,
        )
        if job_id:
            return job_id
    except Exception:  # noqa: BLE001 - rq 装配异常不应影响写路径
        logger.debug("rq 边条件编译入队失败，降级进程内队列", exc_info=True)

    try:
        from app.async_jobs import enqueue_async_job

        job = enqueue_async_job(
            JOB_NAME,
            run_edge_condition_compile,
            tenant_id,
            skill_id,
            agent_id,
            metadata={"tenant_id": tenant_id, "skill_id": skill_id},
        )
        return job.id
    except Exception:  # noqa: BLE001 - 后台编译不是写路径的必要条件
        logger.warning("边条件编译调度失败 tenant=%s skill=%s", tenant_id, skill_id, exc_info=True)
        return None


def edge_condition_review(
    db: Session,
    *,
    tenant_id: str,
    skill_id: str,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """人工复核视图：按**图顺序**列出每条条件边的编译状态。

    这是「人工复核」环节的数据面——前端不解析 JSON 契约，只展示
    ``readable``（结构化条件的可读描述）+ 状态 + 置信度 + 模型给出的理由，
    复核人据此判断是否需要改条件原文或调 LLM 编译。

    图上每条**需要编译**的边都会出现在结果里：
    ``compiled`` 已结构化 / ``llm_judge`` 仍需模型判断 / ``failed`` 编译失败 /
    ``pending`` 尚未编译（刚保存、任务还在队列里）。
    """

    from app.skills.edge_condition_spec import KIND_LABELS, spec_from_payload

    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if row is None:
        return {"skill_id": skill_id, "version": "", "conditions": [], "stats": {}, "pending": 0}

    contents = authoritative_contents(db, row, agent_id)
    # 末位是「运行时真正使用」的那份内容：给了 agent_id 且有分支时是分支投影，
    # 否则就是基础内容（见 authoritative_contents 的追加顺序）。
    content = contents[-1] if contents else {}
    rows = {
        item.condition_fingerprint: item
        for item in db.exec(
            select(SkillEdgeCondition).where(
                SkillEdgeCondition.tenant_id == tenant_id,
                SkillEdgeCondition.skill_id == skill_id,
            )
        ).all()
    }

    nodes = node_index(content)
    conditions: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    pending = 0
    for index, edge in enumerate(iter_edges(content)):
        source_id = str(edge.get("source_node_id") or "").strip()
        next_id = str(edge.get("next_node_id") or "").strip()
        if not source_id or not next_id:
            continue
        condition_text = " ".join(str(edge.get("condition") or "").split()).strip()
        if not condition_text or is_unconditional(condition_text):
            continue
        node = nodes.get(source_id) or {}
        fingerprint = condition_fingerprint(
            source_id, next_id, condition_text, node_required_fields(node)
        )
        stored = rows.get(fingerprint)
        spec = spec_from_payload(stored.spec_json) if stored is not None else None
        if stored is None:
            status = "pending"
            pending += 1
        else:
            status = str(stored.status or "compiled")
        kind = str(getattr(stored, "kind", "") or "") or (spec.kind if spec is not None else "")
        stats[status] = stats.get(status, 0) + 1
        conditions.append(
            {
                "edge_index": index,
                "source_node_id": source_id,
                "source_node_name": str(node.get("name") or ""),
                "next_node_id": next_id,
                "condition": condition_text,
                "status": status,
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind),
                "readable": spec.readable() if spec is not None else "",
                "source": str(getattr(stored, "source", "") or ""),
                "confidence": float(getattr(stored, "confidence", 0.0) or 0.0),
                "rationale": (spec.rationale if spec is not None else ""),
                "error": getattr(stored, "error", None),
            }
        )
    return {
        "skill_id": skill_id,
        "version": row.version,
        "conditions": conditions,
        "stats": stats,
        "pending": pending,
        "total": len(conditions),
    }


def has_conditional_edges(content: Any) -> bool:
    """图里是否存在需要编译的条件边（无条件边不算）。

    写路径用它做短路：纯线性 SOP（全是无条件边）保存时完全不需要入队，
    避免每次保存都产生一个空跑的后台任务。
    """

    if not isinstance(content, dict):
        return False
    for edge in iter_edges(content):
        if str(edge.get("source_node_id") or "").strip() and str(
            edge.get("next_node_id") or ""
        ).strip():
            if not is_unconditional(edge.get("condition")):
                return True
    return False


__all__ = [
    "JOB_FUNC_PATH",
    "JOB_NAME",
    "authoritative_contents",
    "compile_model_config",
    "content_fingerprint",
    "edge_condition_review",
    "edge_fingerprints",
    "has_conditional_edges",
    "has_uncompiled_edge",
    "in_use_fingerprints",
    "kind_stats",
    "prune_outside",
    "run_edge_condition_compile",
    "schedule_edge_condition_compile",
]
