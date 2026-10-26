"""Step 循环模块级辅助（批次 4.6 自 step_loop.py 尾部搬出，零行为改动）。

step_loop.py 统一 re-export，`from app.core.engine.step_loop import X` 路径不变。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError

from app import paths
from app.core.engine.attachments import (
    ValidatedTaskImagePayload,
    isolated_attachment_context,
)
from app.core.base.request_compiler import (
    CapabilityDescriptor,
    TaskExecutionResult,
    TaskRequirement,
)
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.llm.json_field_stream import JsonStringFieldStream
from app.observability.spans import llm_operation
from app.core.knowledge_flow.prefetch import plan_knowledge_prefill_actions
from app.core.sop.step_executor import plan_sop_prefill_actions
from app.session.slot_policy import strip_router_generated_message_slots

from app.core.engine.step_defs import (
    LIGHTWEIGHT_DIRECTIVE,
    MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK,
    MODEL_UNAVAILABLE_FAILURE_CODE,
    PROMPT_PATH,
    TraceSink,
    CancellationCheck,
    HarnessAction,
    HarnessExecutionCancelled,
    HarnessExecutionFenced,
    _ACTION_MODEL_ATTEMPTS,
    _CAPABILITY_RESULT_TRANSCRIPT_MAX_CHARS,
    _KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS,
    _KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS,
    _KNOWLEDGE_TIER_FULL_EVIDENCE,
    _MODEL_CONFIG_ERROR_CODES,
    _MODEL_FAILURE_REASONS,
    _MODEL_TRANSIENT_ERROR_CODES,
    KNOWLEDGE_RESULT_TIERED_INLINE,
    harness_system_prompt,
)


def _requirement_step_id(requirement: TaskRequirement) -> str:
    step = requirement.sop_context.get("step")
    if not isinstance(step, dict):
        return ""
    for key in ("step_id", "node_id", "id"):
        value = str(step.get(key) or "").strip()
        if value:
            return value
    return ""


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _activate_described_capabilities(
    requirement: TaskRequirement,
    tool_name: str,
    result: dict[str, Any],
) -> set[str]:
    if tool_name != "capability_describe" or result.get("success") is not True:
        return set()
    data = result.get("data")
    if (
        not isinstance(data, dict)
        or str(data.get("snapshot_revision") or "")
        != requirement.capability_manifest.snapshot_revision
    ):
        return set()
    raw_descriptors = data.get("activated_capabilities")
    if not isinstance(raw_descriptors, list):
        return set()
    existing = {item.name: item for item in requirement.capability_manifest.available}
    activated: set[str] = set()
    for raw in raw_descriptors:
        if not isinstance(raw, dict):
            continue
        try:
            descriptor = CapabilityDescriptor.model_validate(raw)
        except ValidationError:
            continue
        if not descriptor.available or descriptor.kind == "internal":
            continue
        existing[descriptor.name] = descriptor
        activated.add(descriptor.name)
    requirement.capability_manifest.available = list(existing.values())
    return activated


def _is_loaded_general_skill_result(
    tool_name: str,
    result: dict[str, Any],
) -> bool:
    data = result.get("data")
    return (
        tool_name.startswith("general_skill.")
        and result.get("success") is True
        and isinstance(data, dict)
        and data.get("kind") == "general_skill"
        and data.get("operation") == "read"
    )


def _action_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _is_non_retryable_failure(result: object) -> bool:
    if not isinstance(result, dict) or result.get("success") is not False:
        return False
    error = result.get("error")
    return isinstance(error, dict) and error.get("retryable") is False


def _generate_harness_action_json(
    client: LLMClient,
    system_prompt: str,
    payload: dict[str, Any],
    preview_sink: Callable[[str], None] | None = None,
) -> Any:
    """Use sequence-aware parsing when the concrete client supports it."""

    kwargs: dict[str, Any] = {}
    if preview_sink is not None:
        kwargs["preview_sink"] = preview_sink
    sequence_generator = getattr(client, "generate_json_sequence", None)
    if callable(sequence_generator):
        return sequence_generator(system_prompt, payload, **kwargs)
    # Test doubles and older compatible clients still expose the original method.
    return client.generate_json(system_prompt, payload, **kwargs)


def _reply_preview_sink(emit: Callable[[str], None]) -> Callable[[str], None]:
    """把决策 JSON 的原始文本增量翻译成 ``finish.reply_fragment`` 预览增量。

    预览通道只负责「正文早出现」：结构不符、字段顺序意外都静默放弃，最终正文
    一律以完整 JSON 的解析结果为准（由 ``TurnStreamSink.reconcile`` 收尾对账）。
    """

    extractor = JsonStringFieldStream("reply_fragment", arm_marker="finish")

    def feed(delta: str) -> None:
        text = extractor.feed(delta)
        if text:
            emit(text)

    return feed


def _iteration_reply_preview(
    requirement: TaskRequirement,
    capability_results: list[dict[str, Any]],
    satisfied_required_knowledge_ids: set[str],
    emit: Callable[[str], None] | None,
) -> Callable[[str], None] | None:
    """本次决策调用是否启用正文预览。

    只在「强制能力都已满足」时启用：此时模型要么收尾（``finish``，正文就是最终
    答案），要么再多调一个非强制能力（没有 ``reply_fragment``，预览不会误吐）。
    仍缺强制能力时模型不可能被接受 ``finish``，提前预览只会把一段注定作废的文本
    推给用户，反而要等收尾对账再抹掉。
    """

    if emit is None:
        return None
    if _missing_required_capabilities(
        requirement,
        capability_results,
        satisfied_required_knowledge_ids,
    ):
        return None
    return _reply_preview_sink(emit)


def _harness_actions_from_raw(raw: object) -> list[HarnessAction]:
    """Normalize one action or an ordered action sequence from a provider response."""

    items: object = raw
    if isinstance(raw, dict) and set(raw) == {"actions"}:
        items = raw.get("actions")
    if not isinstance(items, list):
        return [HarnessAction.model_validate(items)]
    if not items:
        raise ValueError("Harness action sequence must not be empty.")
    actions = [HarnessAction.model_validate(item) for item in items]
    if any(action.action == "finish" for action in actions[:-1]):
        raise ValueError("A finish action must be the final item in an action sequence.")
    return actions


def _adapt_general_skill_structured_result(
    raw: object,
    *,
    loaded_general_skill_names: list[str],
) -> HarnessAction | None:
    """Turn an instruction-only Skill's bare business JSON into a safe finish action.

    Skill authors describe the business output contract, not the Harness control
    protocol.  The adapter is deliberately gated on a successfully loaded
    GeneralSkill and never accepts an object that attempted to emit an invalid
    Harness action.  Consequently an RFC/MCP-shaped object is returned as data;
    it is not interpreted or executed as a tool call.
    """

    if not loaded_general_skill_names or not isinstance(raw, (dict, list)):
        return None
    if isinstance(raw, dict) and ({"action", "actions"} & set(raw)):
        return None
    if isinstance(raw, list) and any(
        isinstance(item, dict) and "action" in item for item in raw
    ):
        return None
    reply_fragment = json.dumps(
        raw,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return HarnessAction(
        action="finish",
        status="completed",
        reply_fragment=reply_fragment,
        task_summary=(
            f"{loaded_general_skill_names[-1]} 已生成结构化业务结果。"
        ),
        structured_result=raw,
    )


def _missing_required_capabilities(
    requirement: TaskRequirement,
    capability_results: list[dict[str, Any]],
    satisfied_required_knowledge_ids: set[str],
) -> list[str]:
    succeeded = {
        str(item.get("tool_name") or "")
        for item in capability_results
        if isinstance(item, dict) and item.get("success") is True
    }
    missing = [name for name in requirement.required_capability_names if name not in succeeded]
    for knowledge_base_id in requirement.required_knowledge_base_ids:
        if knowledge_base_id not in satisfied_required_knowledge_ids:
            missing.append(f"knowledge_search:{knowledge_base_id}")
    return missing


def _has_usable_knowledge_evidence(result: dict[str, Any]) -> bool:
    citations = result.get("citations")
    if isinstance(citations, list) and any(isinstance(item, dict) for item in citations):
        return True
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    evidence = data.get("evidence_pack")
    return isinstance(evidence, list) and any(isinstance(item, dict) for item in evidence)


def _finish_result(
    requirement: TaskRequirement,
    action: HarnessAction,
    citations: list[dict[str, Any]],
    evidence_results: list[dict[str, Any]],
    capability_results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    *,
    action_count: int,
) -> TaskExecutionResult:
    status = action.status or "completed"
    step = requirement.sop_context.get("step") if requirement.sop_context else None
    step_type = str(step.get("type") or "").strip() if isinstance(step, dict) else ""
    allowed_next_steps = {
        str(item.get("next_node_id") or "").strip()
        for item in requirement.allowed_transitions
        if isinstance(item, dict) and item.get("next_node_id")
    }
    next_step_id = str(action.next_step_id or "").strip() or None
    if next_step_id and next_step_id not in allowed_next_steps:
        next_step_id = None
    # Merely allowing the optional handoff_human action must not turn an otherwise
    # successful SOP step into a handoff. Even a dedicated handoff node may have a
    # valid non-handoff transition chosen by the model; only a terminal handoff node
    # with no selected successor is coerced to the handoff status.
    if step_type == "handoff" and status == "completed" and next_step_id is None:
        status = "handoff"
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status=status,
        reply_fragment=action.reply_fragment.strip(),
        slot_updates=strip_router_generated_message_slots(action.slot_updates),
        next_step_id=next_step_id,
        citations=citations,
        evidence_results=evidence_results,
        capability_results=capability_results,
        artifacts=artifacts,
        task_summary=action.task_summary.strip(),
        action_count=action_count,
        structured_result=action.structured_result,
        ui_form=action.ui_form,
    )


def _extend_dict_list(target: list[dict[str, Any]], value: object) -> None:
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            target.append(item)


def _raise_if_cancelled(check: CancellationCheck | None) -> None:
    if check is not None and check():
        raise HarnessExecutionCancelled("Harness execution was cancelled.")


def _deadline_expired(deadline_monotonic: float | None) -> bool:
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


def _is_transient_model_error(exc: Exception) -> bool:
    """判断模型/上游侧错误是否值得原样重试（协议与鉴权类错误不在此列）。"""

    if not isinstance(exc, LLMError):
        return False
    code = str(getattr(exc, "code", "") or "")
    if code in _MODEL_TRANSIENT_ERROR_CODES:
        return True
    return bool(getattr(exc, "retryable", False)) and code.startswith("MODEL_")


def _action_generation_failure(
    requirement: TaskRequirement,
    exc: Exception,
    *,
    iteration: int,
    capability_results: list[dict[str, Any]],
) -> TaskExecutionResult:
    """把动作生成失败收尾成用户能看懂的结果。

    模型侧失败（超时/限流/5xx/凭证）与协议失败必须分开：前者此前统一以「当前任务的
    执行模型没有返回有效动作。」收尾，用户等了整轮却看不到任何可操作的提示。
    """

    detail = str(exc)
    if isinstance(exc, LLMError):
        code = str(getattr(exc, "code", "") or "")
        reason = _MODEL_FAILURE_REASONS.get(code, "模型调用失败")
        retried = (
            f"（已重试 {_ACTION_MODEL_ATTEMPTS} 次）" if _is_transient_model_error(exc) else ""
        )
        advice = (
            "请到「模型配置」检查该模型的凭证、接入地址与权限。"
            if code in _MODEL_CONFIG_ERROR_CODES
            else "可以：① 直接重新发送这条消息；② 换用响应更快的模型后重试；"
            "③ 把任务拆成更小的步骤。"
        )
        return TaskExecutionResult(
            task_frame_id=requirement.task_frame_id,
            status="failed",
            reply_fragment=f"本步{reason}{retried}，任务没有完成。{advice}",
            task_summary=f"{reason}，任务未完成。",
            capability_results=capability_results,
            action_count=iteration,
            error={"code": MODEL_UNAVAILABLE_FAILURE_CODE, "message": detail},
        )
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status="failed",
        reply_fragment=(
            "模型没有返回可执行的动作（协议校验失败），请重新发送这条消息，"
            "或更换模型后再试。"
        ),
        task_summary="Harness 动作解析失败。",
        capability_results=capability_results,
        action_count=iteration,
        error={"code": "HARNESS_ACTION_INVALID", "message": detail},
    )


def _step_timeout_result(
    requirement: TaskRequirement,
    *,
    action_count: int,
    timeout_seconds: int | None,
    capability_results: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    evidence_results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    trace_sink: TraceSink | None,
) -> TaskExecutionResult:
    limit_text = f"{timeout_seconds} 秒" if timeout_seconds else "配置的时间"
    error = {
        "code": "SOP_STEP_TIMEOUT",
        "message": f"当前 SOP 单步运行超过 {limit_text}，已停止继续执行。",
        "timeout_seconds": timeout_seconds,
    }
    if trace_sink:
        trace_sink(
            "harness_step_timeout",
            {
                "timeout_seconds": timeout_seconds,
                "action_count": max(0, action_count),
                "error": error,
            },
        )
    return TaskExecutionResult(
        task_frame_id=requirement.task_frame_id,
        status="failed",
        reply_fragment=error["message"],
        citations=citations,
        evidence_results=evidence_results,
        capability_results=capability_results,
        artifacts=artifacts,
        task_summary="SOP 单步运行超时。",
        action_count=max(0, action_count),
        error=error,
    )


def _capability_result_transcript_budget(tool_name: str) -> int:
    """Model-visible tool result budget per capability.

    ``knowledge_search`` 的结果已由 app/knowledge/search_payload 压到 24KB 以内并内联
    （见 KNOWLEDGE_PAYLOAD_MAX_CHARS）；内联预算（10K，I1）小于上游载荷预算，超出部分
    走分级降档（``_tier_knowledge_result_data``），仍超限才退化为截断 preview。
    其他能力沿用 12K 口径不变。
    """

    if tool_name == "knowledge_search":
        return _KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS
    return _CAPABILITY_RESULT_TRANSCRIPT_MAX_CHARS


def _tier_knowledge_result_data(
    data: dict[str, Any],
    *,
    keep_full_evidence: int = _KNOWLEDGE_TIER_FULL_EVIDENCE,
) -> dict[str, Any]:
    """知识检索结果三级降档（I1）。

    ①``evidence`` 前 ``keep_full_evidence`` 条保留正文（各 ≤1.5K）；②其余 evidence
    与全部 ``chunks`` 去掉正文、只留定位索引（chunk_id / 文档 / 页码 / 标题 / 摘要）；
    ③模型需要原文时用已有的 ``read_knowledge_chunk`` 按 chunk_id 取。
    """

    tiered = dict(data)
    evidence = tiered.get("evidence")
    has_evidence = isinstance(evidence, list) and any(
        isinstance(item, dict) for item in evidence
    )
    if isinstance(evidence, list):
        projected: list[Any] = []
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                projected.append(item)
                continue
            entry = dict(item)
            content = entry.get("content")
            if isinstance(content, str) and content:
                if index >= keep_full_evidence:
                    entry.pop("content", None)
                elif len(content) > _KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS:
                    entry["content"] = content[:_KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS]
            projected.append(entry)
        tiered["evidence"] = projected
    chunks = tiered.get("chunks")
    if isinstance(chunks, list):
        # chunks 与 evidence 同源：evidence 在场时正文是副本，一律退化成索引；
        # 无 evidence 时 chunks 自己是正文载体，按同样的 top-N 口径保留。
        def _strip_chunk(item: Any, index: int) -> Any:
            if not isinstance(item, dict):
                return item
            entry = dict(item)
            content = entry.get("content")
            if isinstance(content, str) and content:
                if has_evidence or index >= keep_full_evidence:
                    entry.pop("content", None)
                elif len(content) > _KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS:
                    entry["content"] = content[:_KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS]
            return entry

        tiered["chunks"] = [
            _strip_chunk(item, index) for index, item in enumerate(chunks)
        ]
    return tiered


def _slim_requirement_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """task_requirement 的模型可见投影去重（I4）。

    goal / requirements / completion_criteria 在规划层互相重复表述；判定与
    兜底逻辑仍使用完整版 Pydantic 模型，这里只精简发给模型的字典：
    requirements 去掉与 goal 重复及互相重复的条目，completion_criteria 收成
    一句判定式。
    """

    slimmed = dict(payload)
    goal = str(slimmed.get("goal") or "").strip()

    def _norm(value: Any) -> str:
        return "".join(str(value or "").split())

    goal_key = _norm(goal)
    requirements = [
        str(item or "").strip()
        for item in slimmed.get("requirements") or []
        if str(item or "").strip()
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in requirements:
        key = _norm(item)
        if not key or key in seen:
            continue
        if goal_key and (goal_key in key or key in goal_key):
            continue
        seen.add(key)
        deduped.append(item)
    slimmed["requirements"] = deduped

    criteria = [
        str(item or "").strip()
        for item in slimmed.get("completion_criteria") or []
        if str(item or "").strip()
    ]
    if criteria:
        slimmed["completion_criteria"] = "；".join(dict.fromkeys(criteria))
    return slimmed


def _bounded_capability_result(
    tool_name: str,
    result: dict[str, Any],
    *,
    max_chars: int | None = None,
) -> dict[str, Any]:
    if max_chars is None:
        max_chars = _capability_result_transcript_budget(tool_name)
    payload = {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "data": result.get("data"),
        "error": result.get("error"),
    }
    chunk_count = result.get("chunk_count")
    if isinstance(chunk_count, int) and not isinstance(chunk_count, bool):
        # 小标量随行（场景 C2 判「检索到/没检索到」）；data 超限换文件引用
        # 或整体截断后，data.chunks 都会丢，顶层标量是唯一可靠载体
        payload["chunk_count"] = chunk_count
    if isinstance(result.get("mcp_app"), dict):
        payload["mcp_app"] = result["mcp_app"]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(serialized) <= max_chars:
        return payload
    # I1：知识检索结果超预算先分级降档（top-N 正文 + 其余定位索引），两档
    # 都压不进预算才退化为旧行为的截断 preview。
    if (
        tool_name == "knowledge_search"
        and KNOWLEDGE_RESULT_TIERED_INLINE
        and isinstance(result.get("data"), dict)
    ):
        for keep_full in (_KNOWLEDGE_TIER_FULL_EVIDENCE, 0):
            tiered = dict(payload)
            tiered["data"] = _tier_knowledge_result_data(
                result["data"], keep_full_evidence=keep_full
            )
            tiered_serialized = json.dumps(
                tiered,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            if len(tiered_serialized) <= max_chars:
                return tiered
    truncated = {
        "tool_name": tool_name,
        "success": bool(result.get("success")),
        "truncated": True,
        "preview": serialized[:max_chars],
        "error": result.get("error"),
    }
    if isinstance(chunk_count, int) and not isinstance(chunk_count, bool):
        truncated["chunk_count"] = chunk_count
    if isinstance(result.get("mcp_app"), dict):
        truncated["mcp_app"] = result["mcp_app"]
    return truncated


def _transcript_for_model(
    transcript: list[dict[str, Any]],
    *,
    keep_recent_entries: int = 6,
) -> list[dict[str, Any]]:
    """Project persistent tool history into a bounded execution context.

    Tool results stay in the durable invocation/trace records.  The model only
    needs the newest interactions plus stable receipts for older operations.
    GeneralSkill package instructions are retained because they define the
    workflow the current AgentLoop is following.
    """

    cutoff = max(0, len(transcript) - keep_recent_entries)
    latest_skill_instruction_index: dict[str, int] = {}
    for index, entry in enumerate(transcript):
        if _is_general_skill_instruction_entry(entry):
            latest_skill_instruction_index[str(entry.get("tool_name") or "")] = index

    projected: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(transcript):
        copied = dict(entry)
        skill_name = str(copied.get("tool_name") or "")
        is_latest_skill_instruction = (
            _is_general_skill_instruction_entry(copied)
            and latest_skill_instruction_index.get(skill_name) == index
        )
        if _is_general_skill_instruction_entry(copied) and not is_latest_skill_instruction:
            continue
        if (
            copied.get("role") == "assistant"
            and skill_name.startswith("general_skill.")
            and latest_skill_instruction_index.get(skill_name) != index + 1
        ):
            continue
        if index >= cutoff or is_latest_skill_instruction:
            projected.append((index, copied))
            continue
        if copied.get("role") == "tool" and isinstance(copied.get("result"), dict):
            result = dict(copied["result"])
            receipt: dict[str, Any] = {
                key: result.get(key)
                for key in ("tool_name", "success", "error", "truncated")
                if key in result
            }
            data = result.get("data")
            if isinstance(data, dict):
                receipt["data"] = {
                    key: data.get(key)
                    for key in (
                        "path",
                        "source_path",
                        "destination_path",
                        "sha256",
                        "size",
                        "offset",
                        "next_offset",
                        "continuation_token",
                        "eof",
                        "status",
                        "exit_code",
                    )
                    if data.get(key) is not None
                }
            receipt["history_receipt"] = _history_receipt(result)
            copied["result"] = receipt
        elif copied.get("role") == "assistant":
            arguments = copied.get("arguments")
            if isinstance(arguments, dict):
                copied["arguments"] = _compact_old_arguments(arguments)
        projected.append((index, copied))

    if len(projected) <= 40:
        return [entry for _index, entry in projected]

    # A long-lived non-SOP AgentLoop may exceed the transcript entry cap. Keep
    # the latest loaded instruction for every Skill, then spend the remaining
    # budget on the newest ordinary interactions.
    essential_indexes = {
        index
        for index, entry in projected
        if _is_general_skill_instruction_entry(entry)
    }
    remaining = max(0, 40 - len(essential_indexes))
    ordinary_indexes = [
        index for index, _entry in projected if index not in essential_indexes
    ][-remaining:]
    selected_indexes = essential_indexes | set(ordinary_indexes)
    return [
        entry for index, entry in projected if index in selected_indexes
    ]


def _is_general_skill_instruction_entry(entry: dict[str, Any]) -> bool:
    if entry.get("role") != "tool" or not str(entry.get("tool_name") or "").startswith(
        "general_skill."
    ):
        return False
    result = entry.get("result")
    return isinstance(result, dict) and bool(result.get("success"))


def _history_receipt(value: Any) -> dict[str, Any]:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return {
        "omitted_chars": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _compact_old_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    if len(serialized) <= 1_000:
        return dict(arguments)
    return {"history_receipt": _history_receipt(arguments)}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _trace_capability_result(
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    trace_result = dict(result)
    data = trace_result.get("data")
    if tool_name.startswith("general_skill.") and isinstance(data, dict):
        trace_result["data"] = {
            key: data.get(key)
            for key in (
                "kind",
                "slug",
                "operation",
                "reply",
                "structured_result",
            )
            if data.get(key) not in (None, "", [], {})
        }
    trace = _bounded_capability_result(
        tool_name,
        trace_result,
        max_chars=4_000,
    )
    # 知识检索的 route 观测 phase（route_cache_hit / fast_path 等）是
    # 轻量字符串列表，原样保留到事件 payload 顶层，供诊断脚本扫描
    route_phases = result.get("route_phases")
    if isinstance(route_phases, list) and route_phases:
        trace["route_phases"] = route_phases
    return trace
