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
    _MODEL_CONFIG_ERROR_CODES,
    _MODEL_FAILURE_REASONS,
    _MODEL_TRANSIENT_ERROR_CODES,
    harness_system_prompt,
)
from app.core.engine.step_defs import TraceSink, CancellationCheck  # noqa: F401
from app.core.engine.step_support import (  # noqa: F401  (re-export)
    _requirement_step_id,
    _dict_items,
    _activate_described_capabilities,
    _is_loaded_general_skill_result,
    _action_signature,
    _is_non_retryable_failure,
    _generate_harness_action_json,
    _reply_preview_sink,
    _iteration_reply_preview,
    _harness_actions_from_raw,
    _adapt_general_skill_structured_result,
    _missing_required_capabilities,
    _has_usable_knowledge_evidence,
    _finish_result,
    _extend_dict_list,
    _raise_if_cancelled,
    _is_transient_model_error,
    _action_generation_failure,
    _deadline_expired,
    _step_timeout_result,
    _capability_result_transcript_budget,
    _bounded_capability_result,
    _slim_requirement_payload,
    _transcript_for_model,
    _is_general_skill_instruction_entry,
    _history_receipt,
    _compact_old_arguments,
    _string_list,
    _trace_capability_result,
)


class HarnessTaskAgent:
    """Runs one isolated TaskRequirement without outer conversation messages."""

    def run(
        self,
        requirement: TaskRequirement,
        model_config: ModelConfig,
        invoke_tool: ToolInvoker,
        *,
        max_actions: int = 6,
        trace_sink: TraceSink | None = None,
        is_cancelled: CancellationCheck | None = None,
        image_payloads: list[ValidatedTaskImagePayload] | None = None,
        step_deadline_monotonic: float | None = None,
        step_timeout_seconds: int | None = None,
        checkpoint: dict[str, Any] | None = None,
        lightweight_model_config: ModelConfig | None = None,
        edge_condition_specs: dict[str, Any] | None = None,
        slot_submission: Mapping[str, Any] | None = None,
        laya_router: Any = None,
        reply_preview_sink: Callable[[str], None] | None = None,
    ) -> TaskExecutionResult:
        max_actions = max(1, min(int(max_actions), 100))
        checkpoint = dict(checkpoint or {})
        current_step_id = _requirement_step_id(requirement)
        same_frame = (
            str(checkpoint.get("task_frame_id") or "") == requirement.task_frame_id
        )
        same_step = (
            same_frame
            and str(checkpoint.get("step_id") or "") == current_step_id
        )
        transcript = _dict_items(checkpoint.get("transcript")) if same_frame else []
        citations = _dict_items(checkpoint.get("citations")) if same_frame else []
        evidence_results = (
            _dict_items(checkpoint.get("evidence_results")) if same_frame else []
        )
        capability_results = (
            _dict_items(checkpoint.get("capability_results")) if same_step else []
        )
        satisfied_required_knowledge_ids = set(
            _string_list(checkpoint.get("satisfied_required_knowledge_ids"))
            if same_step
            else []
        )
        successful_knowledge_searches = (
            int(checkpoint.get("successful_knowledge_searches") or 0)
            if same_step
            else 0
        )
        # 产物只挂「本轮真正新产生」的（2026-09-16 修复）：不再从 checkpoint
        # 恢复历史产物——同一 task frame 续轮时，上一轮的产物会被原样复挂到
        # 每条新助手消息的「生成文件」区（典型现场：扫描合同问答里那条空的
        # contract_text.txt 跟着后续每条消息出现）。历史产物的下载不受影响：
        # 下载端点按会话全部消息的 harness_artifacts 检索。
        artifacts: list[dict[str, Any]] = []
        loaded_general_skill_names = (
            _string_list(checkpoint.get("loaded_general_skill_names"))
            if same_frame
            else []
        )
        recent_task_summaries = _string_list(
            checkpoint.get("recent_task_summaries")
        )[-8:]
        # A non-retryable failure only blocks an identical call inside this
        # invocation of the AgentLoop.  Persisting the signature in the
        # checkpoint made a later user turn inherit an obsolete failure even
        # after its inputs or external state had changed.
        non_retryable_action_signatures: set[str] = set()
        allowed_names = requirement.capability_manifest.allowed_names()
        # I2：system prompt 经 lru_cache 缓存，逐字节稳定以命中上游 prompt cache。
        system_prompt = harness_system_prompt()
        pending_actions: list[HarnessAction] = []
        # SOP 确定性执行器（P1）：预检索/纯流转/缺槽询问三类场景由代码直接判定，
        # 预填动作序列跳过首轮 task_action LLM 决策；不可判定返回空，走现有链路。
        # 恢复到同一步骤且上次终点是 awaiting_user（等槽位）时，放行场景 A
        # 槽位抽取——用户带齐信息回来正是该场景的主路径。
        resumed_awaiting_user = (
            same_step
            and str(checkpoint.get("last_status") or "") == "awaiting_user"
        )
        if same_step and not resumed_awaiting_user and callable(trace_sink):
            # 观测点：同步骤恢复但 last_status 不是 awaiting_user → 执行器会被
            # same_step 守卫静默放行给 LLM（2026-09-15 复盘发现补槽 turn 整帧
            # 落回 task_action 的头号嫌疑）。带出 checkpoint 现场，跑一次即定位。
            trace_sink(
                "sop_resume_skipped",
                {
                    "reason": "checkpoint_last_status_not_awaiting_user",
                    "checkpoint_last_status": str(checkpoint.get("last_status") or ""),
                    "checkpoint_step_id": str(checkpoint.get("step_id") or ""),
                    "current_step_id": current_step_id,
                    "checkpoint_keys": sorted(str(k) for k in checkpoint.keys())[:12],
                },
            )
        prefill_actions = plan_sop_prefill_actions(
            requirement,
            same_step=same_step,
            resumed_awaiting_user=resumed_awaiting_user,
            satisfied_required_knowledge_ids=set(satisfied_required_knowledge_ids),
            trace_sink=trace_sink,
            slot_extraction_model=lightweight_model_config or model_config,
            edge_condition_specs=edge_condition_specs,
            slot_submission=slot_submission,
            laya_router=laya_router,
        )
        if prefill_actions:
            pending_actions.extend(
                HarnessAction.model_validate(item) for item in prefill_actions
            )
        # 仅知识库数字员工的确定性直查：新帧首轮直接预填 knowledge_search，
        # 模型第一次被调用时检索结果已在 transcript 里，省掉「决策调检索」
        # 那一整轮 LLM（仅 conversation kind 且无技能/工具能力时介入）。
        # 判定为纯确定性：仅民间词典拦寒暄，其余一律预填（不再调 LLM 判「要不要查」）。
        knowledge_prefill = plan_knowledge_prefill_actions(
            requirement,
            first_turn=not transcript and not evidence_results,
            trace_sink=trace_sink,
        )
        if knowledge_prefill:
            pending_actions.extend(
                HarnessAction.model_validate(item) for item in knowledge_prefill
            )

        def finish(result: TaskExecutionResult) -> TaskExecutionResult:
            summary = " ".join(
                str(result.task_summary or result.reply_fragment or "").split()
            ).strip()
            if (
                result.status != "action_budget"
                and summary
                and (
                    not recent_task_summaries
                    or recent_task_summaries[-1] != summary
                )
            ):
                recent_task_summaries.append(summary[:1_000])
            result.loop_checkpoint = {
                "version": 1,
                "task_frame_id": requirement.task_frame_id,
                "step_id": current_step_id,
                "last_status": result.status,
                "transcript": _transcript_for_model(transcript),
                "citations": citations[-20:],
                "evidence_results": evidence_results[-10:],
                "capability_results": capability_results[-20:],
                "satisfied_required_knowledge_ids": sorted(
                    satisfied_required_knowledge_ids
                ),
                "successful_knowledge_searches": successful_knowledge_searches,
                "artifacts": artifacts[-20:],
                "loaded_general_skill_names": loaded_general_skill_names[-20:],
                "recent_task_summaries": recent_task_summaries[-8:],
            }
            return result

        for iteration in range(1, max_actions + 1):
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration - 1,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))
            # I4：模型可见的 task_requirement 去重（判定逻辑仍用完整版模型）。
            requirement_payload = _slim_requirement_payload(
                requirement.model_dump(mode="json")
            )
            attachment_descriptors, attachment_context = isolated_attachment_context(
                requirement.attachments,
                image_payloads,
            )
            requirement_payload["attachments"] = attachment_descriptors
            payload = {
                "task_requirement": requirement_payload,
                "harness_transcript": _transcript_for_model(transcript),
                "iteration": iteration,
                "remaining_actions": max_actions - iteration + 1,
                "knowledge_search_budget": {
                    "maximum_successful_calls": MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK,
                    "successful_calls": successful_knowledge_searches,
                    "remaining_successful_calls": max(
                        0,
                        MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK
                        - successful_knowledge_searches,
                    ),
                },
            }
            if recent_task_summaries:
                payload["agent_loop_memory"] = {
                    "recent_task_summaries": list(recent_task_summaries),
                }
            if attachment_context is not None:
                payload["conversation_context"] = attachment_context
            try:
                action: HarnessAction | None = None
                validation_error: Exception | None = None
                if pending_actions:
                    action = pending_actions.pop(0)
                else:
                    for protocol_attempt in range(2):
                        # Persist a stable link between this LLM span and the Harness
                        # iteration that consumes it.  Timing projections must not
                        # infer this relationship from overlapping wall-clock windows.
                        # 轻量节点直通：首轮决策尝试用轻量模型 + 一次性编排提示；
                        # 轻量输出协议校验失败时第二轮回退主模型重跑（不再带提示）。
                        first_iteration = iteration == 1
                        using_lightweight = (
                            lightweight_model_config is not None
                            and first_iteration
                            and protocol_attempt == 0
                        )
                        decision_model = (
                            lightweight_model_config if using_lightweight else model_config
                        )
                        directive_payload = (
                            {**payload, "execution_directive": LIGHTWEIGHT_DIRECTIVE}
                            if using_lightweight
                            else payload
                        )
                        with llm_operation(
                            "harness.task_action",
                            task_frame_id=requirement.task_frame_id,
                            iteration=iteration,
                            protocol_attempt=protocol_attempt + 1,
                            lightweight_decision=using_lightweight,
                            # 响应生成链观测：单次 task_action 实测 91-187s，
                            # 需 payload 体量佐证是否上下文过大
                            payload_chars=(
                                len(json.dumps(directive_payload, ensure_ascii=False, default=str))
                                + len(system_prompt or "")
                            ),
                        ):
                            # 上游瞬时失败（超时/限流/5xx）重试同一请求：这类错误与
                            # 协议无关，重试成本远低于让整轮对话以「模型没有返回有效
                            # 动作」收尾。协议/鉴权类错误不在此处重试，直接抛出交给
                            # 外层分类收尾。
                            for model_attempt in range(_ACTION_MODEL_ATTEMPTS):
                                try:
                                    client = _deadline_llm_client(
                                        decision_model,
                                        step_deadline_monotonic,
                                    )
                                    raw = _generate_harness_action_json(
                                        client,
                                        system_prompt,
                                        directive_payload,
                                        preview_sink=_iteration_reply_preview(
                                            requirement,
                                            capability_results,
                                            satisfied_required_knowledge_ids,
                                            reply_preview_sink,
                                        ),
                                    )
                                    break
                                except LLMError as exc:
                                    exhausted = model_attempt + 1 >= _ACTION_MODEL_ATTEMPTS
                                    if (
                                        exhausted
                                        or not _is_transient_model_error(exc)
                                        or _deadline_expired(step_deadline_monotonic)
                                    ):
                                        raise
                                    if trace_sink:
                                        trace_sink(
                                            "harness_action_model_retry",
                                            {
                                                "iteration": iteration,
                                                "attempt": model_attempt + 2,
                                                "max_attempts": _ACTION_MODEL_ATTEMPTS,
                                                "error": str(exc),
                                                "code": str(getattr(exc, "code", "") or ""),
                                            },
                                        )
                        try:
                            actions = _harness_actions_from_raw(raw)
                            action = actions[0]
                            pending_actions.extend(actions[1:])
                            if len(actions) > 1 and trace_sink:
                                trace_sink(
                                    "harness_action_sequence_accepted",
                                    {
                                        "iteration": iteration,
                                        "action_count": len(actions),
                                        "actions": [item.action for item in actions],
                                        "lightweight_decision": using_lightweight,
                                    },
                                )
                        except (ValidationError, ValueError) as exc:
                            action = _adapt_general_skill_structured_result(
                                raw,
                                loaded_general_skill_names=loaded_general_skill_names,
                            )
                            if action is not None:
                                if trace_sink:
                                    trace_sink(
                                        "harness_structured_result_adapted",
                                        {
                                            "iteration": iteration,
                                            "source": loaded_general_skill_names[-1],
                                            "result_type": type(raw).__name__,
                                        },
                                    )
                                break
                            validation_error = exc
                            if protocol_attempt == 0:
                                payload = {
                                    **payload,
                                    "protocol_repair": {
                                        "message": (
                                            "上一次输出不符合 HarnessAction Schema。请只修正动作"
                                            "协议，不要改变任务意图。"
                                        ),
                                        "invalid_output": raw,
                                        "validation_error": str(exc),
                                        "required_tool_envelope": {
                                            "action": "tool",
                                            "tool_name": (
                                                "capability_manifest 中的已授权能力名称"
                                            ),
                                            "arguments": {},
                                        },
                                        "required_finish_envelope": {
                                            "action": "finish",
                                            "status": "completed | awaiting_user | handoff | failed",
                                        },
                                    },
                                }
                                if trace_sink:
                                    trace_sink(
                                        "harness_action_repair_requested",
                                        {
                                            "iteration": iteration,
                                            "error": str(exc),
                                        },
                                    )
                                continue
                            raise
                        break
                if action is None:
                    if validation_error is not None:
                        raise validation_error
                    raise RuntimeError("Harness action generation returned no action.")
            except (ValidationError, ValueError, LLMError) as exc:
                if _deadline_expired(step_deadline_monotonic):
                    return finish(_step_timeout_result(
                        requirement,
                        action_count=iteration - 1,
                        timeout_seconds=step_timeout_seconds,
                        capability_results=capability_results,
                        citations=citations,
                        evidence_results=evidence_results,
                        artifacts=artifacts,
                        trace_sink=trace_sink,
                    ))
                if trace_sink:
                    trace_sink(
                        "harness_action_failed",
                        {
                            "iteration": iteration,
                            "error": str(exc),
                        },
                    )
                return finish(
                    _action_generation_failure(
                        requirement,
                        exc,
                        iteration=iteration,
                        capability_results=capability_results,
                    )
                )
            _raise_if_cancelled(is_cancelled)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))

            if trace_sink:
                trace_sink(
                    "harness_action_created",
                    {
                        "iteration": iteration,
                        "action": action.action,
                        "tool_name": action.tool_name,
                    },
                )
            if action.action == "finish":
                missing_capabilities = _missing_required_capabilities(
                    requirement,
                    capability_results,
                    satisfied_required_knowledge_ids,
                )
                if action.status in {None, "completed"} and missing_capabilities:
                    transcript.extend(
                        [
                            {
                                "role": "assistant",
                                "action": "finish",
                                "status": action.status or "completed",
                            },
                            {
                                "role": "tool",
                                "tool_name": "harness_requirement_check",
                                "result": {
                                    "success": False,
                                    "error": {
                                        "code": "REQUIRED_CAPABILITY_NOT_INVOKED",
                                        "message": (
                                            "当前 SOP 节点尚未成功执行强制能力："
                                            + "、".join(missing_capabilities)
                                        ),
                                    },
                                },
                            },
                        ]
                    )
                    if trace_sink:
                        trace_sink(
                            "harness_completion_blocked",
                            {
                                "iteration": iteration,
                                "reason": "required_capability_not_invoked",
                                "missing_capabilities": missing_capabilities,
                            },
                        )
                    continue
                return finish(_finish_result(
                    requirement,
                    action,
                    citations,
                    evidence_results,
                    capability_results,
                    artifacts,
                    action_count=iteration,
                ))

            tool_name = str(action.tool_name or "").strip()
            if not tool_name or tool_name not in allowed_names:
                transcript.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "result": {
                            "success": False,
                            "error": {
                                "code": "TOOL_NOT_AVAILABLE",
                                "message": "该能力不在当前 TaskFrame 的冻结清单中。",
                            },
                        },
                    }
                )
                continue

            if (
                tool_name == "knowledge_search"
                and successful_knowledge_searches
                >= MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK
            ):
                result = {
                    "success": False,
                    "error": {
                        "code": "KNOWLEDGE_SEARCH_BUDGET_EXHAUSTED",
                        "message": (
                            "当前 TaskFrame 已完成两次有效知识检索。请使用已有证据完成"
                            "原始需求；不要扩展相邻主题或继续改写同义查询。"
                        ),
                    },
                }
            else:
                action_signature = _action_signature(
                    tool_name,
                    dict(action.arguments or {}),
                )
                if action_signature in non_retryable_action_signatures:
                    result = {
                        "success": False,
                        "error": {
                            "code": "NON_RETRYABLE_ACTION_REPEATED",
                            "message": (
                                "相同工具与参数此前已失败且不可重试，本次未再次执行。"
                                "请根据前一次错误更换工具或参数，或结束当前任务。"
                            ),
                            "retryable": False,
                        },
                    }
                    if trace_sink:
                        trace_sink(
                            "harness_action_failed",
                            {
                                "iteration": iteration,
                                "tool_name": tool_name,
                                "error": {
                                    "code": "NON_RETRYABLE_ACTION_REPEATED",
                                    "message": (
                                        "模型重复提交了已标记为不可重试的相同工具调用；"
                                        "调用未执行，AgentLoop 将继续重新规划。"
                                    ),
                                    "retryable": False,
                                },
                            },
                        )
                else:
                    try:
                        _raise_if_cancelled(is_cancelled)
                        result = invoke_tool(tool_name, dict(action.arguments or {}))
                        _raise_if_cancelled(is_cancelled)
                    except (HarnessExecutionCancelled, HarnessExecutionFenced):
                        raise
                    except Exception as exc:
                        result = {
                            "success": False,
                            "error": {
                                "code": "HARNESS_TOOL_ERROR",
                                "message": str(exc),
                            },
                        }
                    if _is_non_retryable_failure(result):
                        non_retryable_action_signatures.add(action_signature)
            bounded_result = _bounded_capability_result(tool_name, result)
            if _is_loaded_general_skill_result(tool_name, result):
                loaded_general_skill_names.append(tool_name)
            transcript.extend(
                [
                    {
                        "role": "assistant",
                        "action": "tool",
                        "tool_name": tool_name,
                        "arguments": action.arguments,
                    },
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "result": bounded_result,
                    },
                ]
            )
            if tool_name not in {"capability_search", "capability_describe"}:
                capability_results.append(bounded_result)
            if _deadline_expired(step_deadline_monotonic):
                return finish(_step_timeout_result(
                    requirement,
                    action_count=iteration,
                    timeout_seconds=step_timeout_seconds,
                    capability_results=capability_results,
                    citations=citations,
                    evidence_results=evidence_results,
                    artifacts=artifacts,
                    trace_sink=trace_sink,
                ))
            activated_names = _activate_described_capabilities(
                requirement,
                tool_name,
                result,
            )
            allowed_names.update(activated_names)
            _extend_dict_list(artifacts, result.get("artifacts"))
            if tool_name == "knowledge_search" and bool(result.get("success")):
                if _has_usable_knowledge_evidence(result):
                    successful_knowledge_searches += 1
                requested_knowledge_ids = _string_list(
                    (action.arguments or {}).get("knowledge_base_ids")
                )
                satisfied_required_knowledge_ids.update(
                    requested_knowledge_ids or requirement.required_knowledge_base_ids
                )
            if (
                tool_name == "knowledge_search"
                and bool(result.get("success"))
                and isinstance(result.get("data"), dict)
            ):
                _extend_dict_list(citations, result.get("citations"))
                evidence_results.append(dict(result["data"]))
            else:
                _extend_dict_list(citations, result.get("citations"))
            if trace_sink:
                trace_payload = {
                    "iteration": iteration,
                    "tool_name": tool_name,
                    "success": bool(result.get("success")),
                    "error": result.get("error"),
                    "result": _trace_capability_result(
                        tool_name,
                        result,
                    ),
                }
                if tool_name == "knowledge_search":
                    # 检索词透传：ks 的 query 是模型改写后的检索词（非用户原话），
                    # 路由缓存按它建 key——观测 hit 具体是哪个问法命中必须带上它
                    trace_payload["query"] = str(
                        (action.arguments or {}).get("query") or ""
                    )[:120]
                trace_sink(
                    "harness_tool_completed",
                    trace_payload,
                )
        return finish(TaskExecutionResult(
            task_frame_id=requirement.task_frame_id,
            status="action_budget",
            reply_fragment="当前任务已达到本轮自动执行上限，需要下一轮继续。",
            citations=citations,
            evidence_results=evidence_results,
            capability_results=capability_results,
            artifacts=artifacts,
            task_summary="Harness 达到 action budget。",
            action_count=max_actions,
            error={"code": "ACTION_BUDGET_EXHAUSTED"},
        ))




def _deadline_llm_client(
    model_config: ModelConfig,
    deadline_monotonic: float | None,
) -> LLMClient:
    if deadline_monotonic is None:
        return LLMClient(model_config)
    remaining = max(deadline_monotonic - time.monotonic(), 0.1)
    configured = getattr(model_config, "timeout_seconds", None)
    timeout_seconds = min(float(configured), remaining) if configured else remaining
    if is_dataclass(model_config):
        limited_config = replace(model_config, timeout_seconds=timeout_seconds)
    else:
        limited_config = model_config.model_copy(
            update={"timeout_seconds": timeout_seconds}
        )
    return LLMClient(limited_config)


