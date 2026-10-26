"""Turn 级执行辅助：SOP 结果合并、引用/产物聚合、会话状态快照、Laya 旁路构造。

批次 4.2 从 ``engine/turn.py`` 尾部搬出的模块级纯函数，零行为改动。
``turn.py`` 统一从这里导入并保持 re-export，测试与 facade 的
``from app.core.engine.turn import X`` 路径不变。
"""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.config import get_settings
from app.core.base.request_compiler import TaskExecutionResult
from app.core.engine.step_loop import (
    MODEL_UNAVAILABLE_FAILURE_CODE,
    HarnessExecutionFenced,
)
from app.core.sop.slots.form import build_slot_form
from app.db.models import (
    ChatSession,
    HarnessTaskFrameRecord,
    Message,
    Skill,
)
from app.knowledge.citations import citation_identity, renumber_citations
from app.session.session_schema import ChatTurnRequest, StepAgentResult

logger = logging.getLogger(__name__)


def _step_result(result: TaskExecutionResult) -> StepAgentResult:
    action = {
        "completed": "advance",
        "awaiting_user": "ask_user",
        "handoff": "handoff",
        "failed": "reply",
        "blocked": "reply",
        "action_budget": "reply",
    }.get(result.status, "reply")
    return StepAgentResult(
        action=action,  # type: ignore[arg-type]
        reply=result.reply_fragment,
        slot_updates=dict(result.slot_updates),
        knowledge_results=list(result.evidence_results),
        next_step_id=result.next_step_id,
        is_step_completed=result.status == "completed",
        handoff=result.status == "handoff",
        structured_result=result.structured_result,
    )


# 只消耗模型调用、不消耗业务状态的失败码：这些情况下已完成节点的槽位与转移都已
# 落库，AgentLoop 可以原样续跑而不丢用户可见结果。
_RECOVERABLE_ACTION_FAILURE_CODES = frozenset(
    {
        "HARNESS_ACTION_INVALID",
        MODEL_UNAVAILABLE_FAILURE_CODE,
    }
)


def _is_recoverable_action_protocol_failure(result: TaskExecutionResult) -> bool:
    """Keep a SOP AgentLoop resumable when only the model action envelope is invalid.

    模型/上游瞬时不可用（超时、限流、5xx）同样不消耗业务状态，必须和协议失败一样
    保持 AgentLoop 可续跑；否则一次网关抖动就会把整个 SOP 步骤判死。
    """

    error = result.error if isinstance(result.error, dict) else {}
    return result.status == "failed" and str(error.get("code") or "") in (
        _RECOVERABLE_ACTION_FAILURE_CODES
    )


def _defer_failed_step_after_completed_checkpoint(
    result: TaskExecutionResult,
    completed_results: list[TaskExecutionResult],
) -> TaskExecutionResult:
    """Keep a committed SOP result when immediate continuation fails.

    SOP TaskFrames may advance through several nodes in one request.  Once a
    node has completed, its transition and slots are already durable.  A model
    or protocol failure while starting the following node must therefore not
    erase a user-visible result from the completed node.  Represent the
    unfinished continuation as ``action_budget`` so the frame remains queued
    and resumable at the already-persisted next step.
    """

    if result.status != "failed":
        return result
    checkpoint = next(
        (
            item
            for item in reversed(completed_results)
            if item.status == "completed" and item.reply_fragment.strip()
        ),
        None,
    )
    if checkpoint is None:
        return result
    failure_summary = str(result.task_summary or "").strip()
    summaries = [
        summary
        for summary in (checkpoint.task_summary.strip(), failure_summary)
        if summary
    ]
    return result.model_copy(
        update={
            "status": "action_budget",
            "reply_fragment": checkpoint.reply_fragment,
            "next_step_id": None,
            "task_summary": "；".join(dict.fromkeys(summaries)),
        }
    )


def _enforce_required_slots(
    result: TaskExecutionResult,
    requirement: Any,
    session: ChatSession,
) -> TaskExecutionResult:
    if result.status != "completed" or not requirement.required_slots:
        return result
    merged = {
        **dict(session.slots_json or {}),
        **dict(result.slot_updates or {}),
    }
    missing = [
        field
        for field in requirement.required_slots
        if merged.get(field) in (None, "", [], {})
    ]
    if not missing:
        return result
    result.status = "awaiting_user"
    if not result.reply_fragment:
        result.reply_fragment = "还需要您补充：" + "、".join(missing) + "。"
    result.next_step_id = None
    # A2UI：这条 awaiting 是「模型以为办完了、其实还缺槽」的兜底改写，同样要
    # 给前端一份表单描述——否则这种路径下用户面对的还是「自己猜格式」的纯
    # 文本询问，与确定性缺槽路径的体验割裂。
    result.ui_form = result.ui_form or _slot_form_for_requirement(requirement, missing)
    return result


def _slot_form_for_requirement(
    requirement: Any,
    missing: list[str],
) -> dict[str, Any] | None:
    """按 TaskRequirement 的 SOP 上下文编译缺槽表单（拿不到选项就退化成输入框）。"""

    if not missing:
        return None
    context = getattr(requirement, "sop_context", None)
    context = context if isinstance(context, dict) else {}
    step = context.get("step")
    step = step if isinstance(step, dict) else {}
    labels = context.get("slot_labels")
    return build_slot_form(
        missing,
        labels=labels if isinstance(labels, dict) else None,
        step_name=str(step.get("name") or "").strip(),
        step_id=str(step.get("node_id") or step.get("step_id") or "").strip(),
        skill_id=str(context.get("skill_id") or "").strip(),
    )


def _combine_results(
    task_frame_id: str,
    results: list[TaskExecutionResult],
) -> TaskExecutionResult:
    if not results:
        return TaskExecutionResult(
            task_frame_id=task_frame_id,
            status="failed",
            reply_fragment="当前 TaskFrame 未产生执行结果。",
            error={"code": "EMPTY_TASK_RESULT"},
        )
    last = results[-1]
    terminal_reply = next(
        (
            item.reply_fragment.strip()
            for item in reversed(results)
            if item.reply_fragment.strip()
        ),
        "",
    )
    return TaskExecutionResult(
        task_frame_id=task_frame_id,
        status=last.status,
        # A SOP TaskFrame can advance through several steps in one turn.  Each
        # step produces a finish reply, but only the terminal step is the
        # user-visible answer for that TaskFrame.  Keep intermediate summaries
        # and structured results below without concatenating their transitional
        # replies into a duplicated final response.
        reply_fragment=terminal_reply,
        slot_updates={
            key: value
            for item in results
            for key, value in item.slot_updates.items()
        },
        next_step_id=last.next_step_id,
        citations=[
            citation
            for item in results
            for citation in item.citations
        ],
        evidence_results=[
            evidence
            for item in results
            for evidence in item.evidence_results
        ],
        capability_results=[
            capability_result
            for item in results
            for capability_result in item.capability_results
        ],
        structured_result=last.structured_result,
        artifacts=[
            artifact
            for item in results
            for artifact in item.artifacts
        ],
        task_summary="；".join(
            dict.fromkeys(
                item.task_summary.strip()
                for item in results
                if item.task_summary.strip()
            )
        ),
        action_count=sum(item.action_count for item in results),
        error=last.error,
        # A2UI：合并时**必须**带上表单描述。``_combine_results`` 是逐帧结果的
        # 唯一出口，frame 内每一步的结果都在这里被摊平成一条——漏掉 ui_form
        # 会让缺槽询问退回纯文本（实测踩中：DB 里 metadata_json 完全没有
        # ``a2ui`` 键，而 trace 已经走到 await_user_for_slots 场景）。
        # 口径与 structured_result / error 一致：取**终点**结果的那一份，
        # 因为只有终点的状态才是这条 frame 的最终状态。
        ui_form=last.ui_form,
    )


def _single_task_reply(results: list[TaskExecutionResult]) -> str | None:
    """Use a lone TaskFrame's terminal reply without another model pass.

    ``finish`` already asks the Harness model for the user-facing reply.  A
    response synthesis pass is only useful when several TaskFrames must be
    reconciled.  Empty replies still fall back to ``ResponseGenerator`` so
    malformed or legacy execution results keep the existing recovery path.
    """

    if len(results) != 1:
        return None
    result = results[0]
    reply = str(result.reply_fragment or "").strip()
    if _structured_reply_requires_synthesis(reply, result.structured_result):
        return None
    return reply or None


def _structured_reply_requires_synthesis(reply: str, structured_result: Any) -> bool:
    """Reject an obviously partial JSON projection when complete data is available."""

    if structured_result is None or not reply:
        return False
    looks_like_json = reply.startswith("{") or (
        reply.startswith("[")
        and (len(reply) == 1 or reply[1:2] in {"{", "[", '"'})
    )
    if not looks_like_json:
        return False
    try:
        projected = json.loads(reply)
    except (json.JSONDecodeError, TypeError):
        return True
    return projected != structured_result


def _response_task_payload(
    row: HarnessTaskFrameRecord,
    result: TaskExecutionResult,
    skill: Skill | None,
    step_result: StepAgentResult,
) -> dict[str, object]:
    projected_step_result = step_result.model_dump(mode="json")
    projected_step_result["reply"] = result.reply_fragment
    projected_step_result["knowledge_results"] = list(result.evidence_results)
    return {
        "task": row.user_intent or "当前任务",
        "task_frame_id": row.task_id,
        "status": result.status,
        "skill_content": dict(skill.content_json or {}) if skill else {},
        "current_step_id": row.step_id,
        "slots": dict(row.slots_json or {}),
        "step_result": projected_step_result,
        "tool_result": (
            {
                "success": all(
                    bool(item.get("success"))
                    for item in result.capability_results
                ),
                "capability_results": list(result.capability_results),
            }
            if result.capability_results
            else None
        ),
        "task_summary": result.task_summary,
        "structured_result": result.structured_result,
        "artifacts": list(result.artifacts),
    }


def _inject_handoff_context(
    db: object,
    session: ChatSession,
    payloads: list[dict[str, object]],
    results: list[TaskExecutionResult],
    request: ChatTurnRequest | None = None,
) -> None:
    """在 resume turn 执行期间注入 handoff_info(含 human_reply),供 response_generator 转述。

    判定 resume turn:request.channel == "human_handoff_resume"(由 _resume_human_handoff_worker
    传入)。此时 handoff 已 answered 且 human_reply 已落库,直接注入即可——不再依赖
    resume_finished_at 标记(原标记在 worker 写入时机晚于 turn 执行,导致注入永远 miss)。
    handoff 状态的 task(本轮新触发转人工)也注入 handoff_info(无 human_reply),
    用于告知用户已转交。
    """
    from app.db.models import HumanHandoffRequest

    handoff = db.exec(
        select(HumanHandoffRequest)
        .where(HumanHandoffRequest.session_id == session.id)
        .order_by(HumanHandoffRequest.created_at.desc())
    ).first()
    if not handoff:
        return
    notify_message_id = handoff.notify_message_id or ""
    human_reply = (handoff.human_reply or "").strip()
    is_answered = handoff.status in ("answered", "resolved") and bool(human_reply)
    # resume turn:由 worker 显式传入 channel 标识判定,时序可靠。
    is_resume_turn = (
        bool(request and getattr(request, "channel", "") == "human_handoff_resume")
        and is_answered
    )
    for payload, result in zip(payloads, results, strict=False):
        if is_resume_turn or payload.get("status") == "handoff":
            pass
        else:
            continue
        handoff_info: dict[str, Any] = {
            "handoff_id": handoff.id,
            "notified_via_channel": bool(notify_message_id),
        }
        if is_answered:
            handoff_info["human_reply"] = human_reply
        payload["handoff_info"] = handoff_info


_MAX_GLOBAL_CITATIONS = 8


def _globalize_citations(
    results: list[TaskExecutionResult],
) -> list[dict[str, Any]]:
    """把各任务帧的引用合成一份全局编号表。

    编号必须全局连续且同一条证据只占一个号：各帧的 citations 各自从 ``kref_1``
    起编号，直接拼起来会撞 ``id``（前端拿它当 React key），同一证据被两个知识库
    版本重复收录时还会白占两个号——正文里的 ``[n]`` 就找不到对应卡片了。
    """
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for citation in result.citations:
            identity = citation_identity(citation)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            if len(unique) >= _MAX_GLOBAL_CITATIONS:
                continue
            unique.append(citation)
    numbered = renumber_citations(unique)
    by_identity = {citation_identity(item): item for item in numbered}
    for result in results:
        deduped: list[dict[str, Any]] = []
        seen_in_frame: set[str] = set()
        for citation in result.citations:
            identity = citation_identity(citation)
            shared = by_identity.get(identity)
            if shared is None or identity in seen_in_frame:
                continue
            seen_in_frame.add(identity)
            deduped.append(shared)
        result.citations = deduped
    return numbered


def _aggregate_artifacts(
    results: list[TaskExecutionResult],
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for artifact in result.artifacts:
            identity = "|".join(
                str(artifact.get(field) or "")
                for field in ("type", "task_frame_id", "path", "handoff_id")
            )
            if not identity.strip("|") or identity in seen:
                continue
            seen.add(identity)
            artifacts.append(dict(artifact))
            if len(artifacts) >= 20:
                return artifacts
    return artifacts


def _merge_discovered_artifacts(
    result: TaskExecutionResult,
    discovered: list[dict[str, Any]],
) -> None:
    known_paths = {
        str(item.get("path") or "")
        for item in result.artifacts
        if isinstance(item, dict) and item.get("path")
    }
    for item in discovered:
        path = str(item.get("path") or "")
        if not path or path in known_paths:
            continue
        result.artifacts.append(dict(item))
        known_paths.add(path)
        if len(result.artifacts) >= 20:
            break


def _append_session_handoff_artifact(
    result: TaskExecutionResult,
    session: ChatSession,
) -> None:
    awaiting = (
        session.awaiting_input_json
        if isinstance(session.awaiting_input_json, dict)
        else {}
    )
    handoff_id = str(awaiting.get("handoff_id") or "").strip()
    if not handoff_id:
        return
    result.artifacts.append(
        {
            "type": "human_handoff",
            "handoff_id": handoff_id,
        }
    )


def _with_recoverable_first_session(
    request: ChatTurnRequest,
) -> ChatTurnRequest:
    """Derive a stable first-turn session when the caller supplied a turn id."""

    if request.session_id or not str(request.client_turn_id or "").strip():
        return request
    identity = "\x1f".join(
        (
            request.tenant_id.strip(),
            str(request.user_id or "").strip(),
            str(request.client_turn_id or "").strip(),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return request.model_copy(
        update={"session_id": f"session_{digest[:16]}"}
    )


def get_or_create_harness_session(
    owner: Any,
    request: ChatTurnRequest,
) -> ChatSession:
    """Create or recover one Harness session, including concurrent PK races."""

    db = owner.db
    try:
        session = owner._get_or_create_session(request)
        db.commit()
    except IntegrityError:
        # Two workers may race to create the same deterministic first-turn
        # session. The loser reuses the row committed by the winner.
        db.rollback()
        session = (
            db.get(ChatSession, request.session_id)
            if request.session_id
            else None
        )
        if session is None:
            raise

    if session.tenant_id != request.tenant_id:
        raise HarnessExecutionFenced(
            "Harness session tenant does not match the request."
        )
    if request.user_id is not None and session.user_id != request.user_id:
        raise HarnessExecutionFenced(
            "Harness session user does not match the request."
        )
    if (
        request.agent_id is not None
        and session.agent_id not in {None, request.agent_id}
    ):
        raise HarnessExecutionFenced(
            "Harness session agent does not match the request."
        )
    if request.agent_id and not session.agent_id:
        session.agent_id = request.agent_id
        db.add(session)
        db.commit()
    db.refresh(session)
    return session


def _session_state(session: ChatSession) -> dict[str, Any]:
    return {
        "active_skill_id": session.active_skill_id,
        "active_step_id": session.active_step_id,
        "slots_json": deepcopy(session.slots_json or {}),
        "awaiting_input_json": deepcopy(session.awaiting_input_json),
        "context_state_json": deepcopy(session.context_state_json or {}),
        "summary": session.summary,
        "last_agent_question": session.last_agent_question,
    }


def _restore_session_state(
    session: ChatSession, state: dict[str, Any]
) -> None:
    session.active_skill_id = state.get("active_skill_id")
    session.active_step_id = state.get("active_step_id")
    session.slots_json = deepcopy(state.get("slots_json") or {})
    session.awaiting_input_json = deepcopy(state.get("awaiting_input_json"))
    session.context_state_json = deepcopy(state.get("context_state_json") or {})
    session.summary = state.get("summary")
    session.last_agent_question = state.get("last_agent_question")


def _build_laya_router(frame_kind: str) -> Any:
    """按配置构造 Laya 出边决策旁路；未开启或非 sop 帧返回 None。

    每帧（每次 _run_frame）构造一个实例，熔断状态因此限定在单帧内，
    下一帧自动恢复；独立短超时不影响页面「决策助手」的 60s 配置。
    """

    if frame_kind != "sop":
        return None
    settings = get_settings()
    if not settings.laya_sop_routing_enabled:
        return None
    try:
        from app.core.knowledge_flow.routing import LayaRouter
    except Exception:  # noqa: BLE001 - 旁路不可用不影响主链路
        logger.debug("Laya 旁路加载失败", exc_info=True)
        return None
    return LayaRouter(
        enabled=True,
        shadow=settings.laya_sop_shadow_mode,
        min_confidence=settings.laya_min_confidence,
        timeout=settings.laya_sop_timeout_seconds,
        max_failures=settings.laya_sop_circuit_breaker_failures,
    )


def _build_knowledge_laya_router() -> Any:
    """按配置构造 Laya 知识路由（选文档 / 选桶）旁路；未开启返回 None。

    与 SOP 旁路分开实例：启用开关、影子模式、熔断状态都独立，
    知识路由不可用不影响 SOP 出边决策，反之亦然。每帧构造一个。
    """

    settings = get_settings()
    if not settings.laya_knowledge_route_enabled:
        return None
    try:
        from app.core.knowledge_flow.routing import LayaRouter
    except Exception:  # noqa: BLE001 - 旁路不可用不影响主链路
        logger.debug("Laya 知识路由旁路加载失败", exc_info=True)
        return None
    return LayaRouter(
        enabled=True,
        shadow=settings.laya_knowledge_shadow_mode,
        min_confidence=settings.laya_min_confidence,
        timeout=settings.laya_knowledge_timeout_seconds,
        max_failures=settings.laya_sop_circuit_breaker_failures,
        max_candidates=settings.laya_knowledge_max_candidates,
        questions_per_call=settings.laya_knowledge_questions_per_call,
    )


def _load_edge_condition_specs(
    db: Any,
    tenant_id: str,
    active_skill: Skill | None,
    frame_kind: str,
) -> dict[str, Any]:
    """加载 SOP 边条件结构化编译产物，键为条件指纹。

    只在 sop 帧、有技能时查库；任何异常返回空字典——编译产物是**加速项**，
    缺失时运行时自然回落到 LLM 决策，不应该让一次查询失败影响对话。
    """

    if frame_kind != "sop" or active_skill is None:
        return {}
    skill_id = str(getattr(active_skill, "skill_id", "") or "")
    if not skill_id:
        return {}
    try:
        from app.skills.edge_condition_compiler import load_edge_condition_specs

        return load_edge_condition_specs(db, tenant_id, skill_id)
    except Exception:  # noqa: BLE001 - 加速项加载失败不阻断主链路
        logger.debug("SOP 边条件编译产物加载失败 skill=%s", skill_id, exc_info=True)
        return {}


def _prior_result(result: TaskExecutionResult) -> dict[str, Any]:
    return {
        "task_frame_id": result.task_frame_id,
        "status": result.status,
        "task_summary": result.task_summary,
        "slot_updates": result.slot_updates,
        "capability_results": result.capability_results,
        "artifacts": result.artifacts,
        "structured_result": result.structured_result,
    }


def _source_user_message(db: Any, row: HarnessTaskFrameRecord) -> str:
    message = db.get(Message, row.source_turn_id)
    if message is None or message.role != "user":
        return ""
    return str(message.content or "").strip()


def _sibling_task_intents(
    db: Any,
    row: HarnessTaskFrameRecord,
) -> list[str]:
    """Return work from the same user turn that belongs to another TaskFrame."""

    siblings = db.exec(
        select(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.session_id == row.session_id,
            HarnessTaskFrameRecord.source_turn_id == row.source_turn_id,
            HarnessTaskFrameRecord.id != row.id,
        )
    ).all()
    return [
        str(item.user_intent or "").strip()
        for item in siblings
        if str(item.user_intent or "").strip()
    ]


def _skill_step_timeout_seconds(skill: Skill | None) -> int | None:
    if skill is None or not isinstance(skill.content_json, dict):
        return None
    raw = skill.content_json.get("step_timeout_seconds")
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, 3600))


def _dependency_order(
    records: list[HarnessTaskFrameRecord],
) -> list[HarnessTaskFrameRecord]:
    by_id = {row.task_id: row for row in records}
    remaining = list(records)
    ordered: list[HarnessTaskFrameRecord] = []
    resolved: set[str] = set()
    while remaining:
        ready = [
            row
            for row in remaining
            if all(
                dependency_id in resolved
                or dependency_id not in by_id
                for dependency_id in row.depends_on_json or []
            )
        ]
        if not ready:
            ordered.extend(remaining)
            break
        for row in ready:
            ordered.append(row)
            resolved.add(row.task_id)
            remaining.remove(row)
    return ordered
