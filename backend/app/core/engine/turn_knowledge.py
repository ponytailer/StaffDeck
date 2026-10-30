"""Turn 知识工作流：仅知识库直答、知识作答、结果综合（批次 4.3 自 turn.py 类体搬出，mixin 化，零行为改动）。"""

from __future__ import annotations

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.aigw_cache import get_json, set_json
from app.config import get_settings
from app.core.base.cancellation import is_chat_turn_cancelled
from app.core.capability.discovery import project_capability_manifest
from app.core.capability.manifest import CapabilityManifestBuilder
from app.core.base.conversation_projection import ConversationProjection
from app.core.engine.step_loop import (
    MODEL_UNAVAILABLE_FAILURE_CODE,
    HarnessExecutionCancelled,
    HarnessExecutionFenced,
    HarnessTaskAgent,
    _trace_capability_result,
)
from app.core.engine.attachments import (
    materialize_task_attachments,
    validated_task_image_payloads,
)
from app.core.engine.invoke import HarnessCapabilityInvoker
from app.core.knowledge_flow.prefetch import is_smalltalk
from app.core.capability.deliverables import list_published_deliverables
from app.core.engine.session import (
    HarnessSessionLeaseLost,
    HarnessSessionLeaseStore,
    HarnessSessionLeaseToken,
)
from app.core.engine.session import (
    acquire_harness_session,
    release_harness_session,
)
from app.core.engine.session import HarnessTurnStore
from app.core.plan.slash import (
    SlashCommandError,
    SlashCommandSelection,
    build_slash_turn_plan,
    force_capability_for_requirement,
    parse_slash_command,
    resolve_capability,
    slash_command_message,
)
from app.core.sop.slots.form import build_slot_form
from app.core.sop.slots.hydration import SlotHydrationPolicy
from app.core.engine.frames import (
    TaskFrameClaimConflict,
    TaskFrameStore,
    planned_frame_from_record,
)
from app.core.base.request_compiler import (
    TaskExecutionResult,
    TaskRequestCompiler,
)
from app.core.plan.planner import TurnPlanner, turn_plan_router_decision
from app.core.base.ui_flags import knowledge_citations_hidden
from app.db.models import (
    ChatSession,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
    Skill,
    Team,
    utc_now,
)
from app.knowledge.citations import (
    citation_identity,
    compact_knowledge_citation_labels,
    renumber_citations,
    strip_citation_markers,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.memory.service import memory_read
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    StepAgentResult,
    TurnPlan,
)
from app.skills.nesting import discoverable_sops, expand_visible_sops

from app.core.engine.turn_support import (

    _aggregate_artifacts,
    _append_session_handoff_artifact,
    _build_knowledge_laya_router,
    _build_laya_router,
    _combine_results,
    _defer_failed_step_after_completed_checkpoint,
    _dependency_order,
    _enforce_required_slots,
    _globalize_citations,
    _inject_handoff_context,
    _is_recoverable_action_protocol_failure,
    _load_edge_condition_specs,
    _merge_discovered_artifacts,
    _prior_result,
    _response_task_payload,
    _restore_session_state,
    _session_state,
    _single_task_reply,
    _skill_step_timeout_seconds,
    _sibling_task_intents,
    _source_user_message,
    _step_result,
    _with_recoverable_first_session,
    get_or_create_harness_session,
)

from app.core.engine.turn_context import (
    INTENT_CACHE_TTL_SECONDS,
    KNOWLEDGE_ANSWER_OPERATION,
    _KNOWLEDGE_WORKFLOW_BLOCKING_KINDS,
    _TRACE_STREAM_STATUS,
    _apply_forced_sop_snapshot,
    _capability_summary_text,
    _direct_reply_answer,
    _direct_reply_answer_stream,
    _intent_cache_key,
    _is_knowledge_workflow_frame,
    _session_title_from_plan,
    _stream_status_for_trace,
    _turn_planner_message,
    _turn_skill_projection,
    _turn_slash_selection,
)


logger = logging.getLogger(__name__)

# ---- 互联网检索判定（「何时该检索 / 何时本地知识库够用」）----
# 用户明确要求：开关打开后不能无脑联网，也不能无脑用本地库。
# 这里的判定是**确定性规则**而不是再调一次 LLM —— 本会话刚修完
# 「知识库问答多烧 3 次 LLM」的问题，绝不能为省一次检索又加一次调用。

#: 本地证据「够用」的最小命中条数。1 条孤证撑不起回答（可能是恰好沾边）。
_KNOWLEDGE_SUFFICIENT_MIN_CHUNKS = 2
#: 本地证据「够用」的正文总量下限。低于它说明命中的是标题/目录片段，
#: 正文不完整，需要联网补充。
_KNOWLEDGE_SUFFICIENT_MIN_CONTENT_CHARS = 300
#: 时效性信号词：命中即认为本地知识库**必然**过期（库是静态快照），
#: 即使有命中也要联网核对。宁可多搜一次，不可拿旧数据冒充现状。
_FRESHNESS_MARKERS = (
    "最新",
    "今天",
    "昨天",
    "本周",
    "上周",
    "这个月",
    "最近",
    "现在",
    "目前",
    "今年",
    "去年",
    "实时",
    "当前版本",
    "新版",
    "价格",
    "报价",
    "汇率",
    "股价",
    "天气",
    "新闻",
    "发布了",
)


def _knowledge_evidence_sufficient(search_result: dict[str, Any]) -> bool:
    """本地知识库召回是否足以支撑回答（不调 LLM 的确定性判定）。"""

    data = search_result.get("data")
    if not isinstance(data, dict):
        return False
    chunks = data.get("chunks")
    if not isinstance(chunks, list):
        chunks = data.get("evidence_pack")
    if not isinstance(chunks, list):
        return False
    if len(chunks) < _KNOWLEDGE_SUFFICIENT_MIN_CHUNKS:
        return False
    content_chars = sum(
        len(str(item.get("content") or "")) for item in chunks if isinstance(item, dict)
    )
    return content_chars >= _KNOWLEDGE_SUFFICIENT_MIN_CONTENT_CHARS


def _query_needs_fresh_data(message: str) -> bool:
    """query 是否带时效性信号 —— 本地知识库对这类问题天然过期。"""

    text = message or ""
    return any(marker in text for marker in _FRESHNESS_MARKERS)


class _KnowledgeWorkflowMixin:
    def _answer_direct_reply(
        self,
        message: str,
        persona_prompt: str | None,
        memory_context: list[dict[str, Any]],
        conversation_context: dict[str, Any] | None,
        answer_model: Any,
        *,
        capability_summary: str | None = None,
    ) -> str:
        """direct_reply / capability_intro 直出回复。

        2026-10-19：不再走真流式预览通道（预览会让正文跟随上游分包抖动），
        改为一次性生成，由 `_handle_turn_stream_v2` 的 8 字/45ms 匀速回放下发。
        """

        self._emit_stream_status("preparing", "正在生成回复")
        return _direct_reply_answer(
            message,
            persona_prompt,
            memory_context,
            conversation_context,
            answer_model,
            capability_summary=capability_summary,
        )

    def _run_knowledge_workflow(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
        row: HarnessTaskFrameRecord,
        frame: Any,
        requirement: Any,
        invoker: HarnessCapabilityInvoker,
        model_config: Any,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, Any] | None,
        trace: Any,
    ) -> TaskExecutionResult | None:
        """仅知识库员工的确定性工作流：寒暄直回 / 代码直调检索 → 一次 LLM 作答。

        不走 Harness 决策循环：检索由代码直接调 knowledge_search（复用
        HarnessCapabilityInvoker 的授权/版本/Laya 路由），随后把证据与 prompt
        拼给 ResponseGenerator 一次生成正文。任何环节异常返回 None，由调用方
        降级回标准 Harness 路径（与 _run_direct_reply_frame 同约定）。
        """

        message = str(requirement.source_user_message or "").strip()
        persona_prompt = self.owner._get_persona_prompt(
            request.tenant_id, session.agent_id
        )
        try:
            # 1) 寒暄：不检索，直接社交回复（0 检索 + 1 次 LLM）
            if is_smalltalk(message):
                reply = self._answer_direct_reply(
                    message,
                    persona_prompt,
                    memory_context,
                    conversation_context,
                    model_config,
                )
                trace("knowledge_workflow_smalltalk", {"query": message})
                return TaskExecutionResult(
                    task_frame_id=row.task_id,
                    status="completed",
                    reply_fragment=reply,
                    task_summary="知识库工作流：寒暄直回。",
                    action_count=1,
                )

            # 2) 代码直调检索（不走 LLM 决策轮）
            # ``inline_result=True``：这条路径的 data 只在进程内用（拼回答上下文与
            # 引用卡片），不进模型 transcript。不传的话，命中条数多到精简载荷超出
            # 内联预算时，data 会被换成 ``kind=sandbox_json_file`` 指针，下面的
            # 「data 是不是 dict」判定立刻失败 → return None → 降级回 Harness 决策
            # 循环。实测「打印机怎么用」7 条命中即触发：25170 > 24000 预算。
            search_result = invoker.invoke(
                "knowledge_search",
                {"query": message[:200], "max_chunks": 8},
                inline_result=True,
            )
            if not isinstance(search_result, dict) or search_result.get("success") is not True:
                trace(
                    "knowledge_workflow_search_failed",
                    {
                        "query": message,
                        "error": (search_result or {}).get("error")
                        if isinstance(search_result, dict)
                        else None,
                    },
                )
                return None
            data = search_result.get("data")
            # data 被大 payload 机制换成沙箱文件指针时拿不到证据 → 降级标准路径
            if not isinstance(data, dict) or not (
                isinstance(data.get("chunks"), list)
                or isinstance(data.get("evidence_pack"), list)
            ):
                trace("knowledge_workflow_payload_unusable", {"query": message})
                return None
            citations = list(search_result.get("citations") or [])
            # 与 Harness 路径同口径的观测事件：诊断脚本/会话耗时投影靠
            # harness_tool_completed.result.route_phases 扫描路由命中。
            trace(
                "harness_tool_completed",
                {
                    "iteration": 1,
                    "tool_name": "knowledge_search",
                    "success": True,
                    "error": None,
                    "result": _trace_capability_result("knowledge_search", search_result),
                    "query": message[:120],
                    "workflow": "knowledge_direct",
                },
            )

            # 3) 互联网检索判定：本地够用就不联网，不够用才补一次。
            #    前提是 web_search 在冻结清单里（员工开关打开时 manifest 才有）。
            #    判定顺序刻意是「先本地后联网」：
            #    - 本地证据够 → 0 次联网请求，回答全部有企业内引用（现状不变）；
            #    - 本地不足 → 补一次 web_search，与知识库证据合并后一次作答
            #      （仍然只有 1 次 LLM 调用）；
            #    - query 带时效性信号（最新/价格/天气…）→ 本地库是静态快照
            #      必然过期，即使有命中也联网核对。
            web_payload: dict[str, Any] | None = None
            web_capable = "web_search" in getattr(
                invoker, "available_capability_names", set()
            )
            if web_capable:
                local_ok = _knowledge_evidence_sufficient(search_result)
                stale_query = _query_needs_fresh_data(message)
                if not local_ok or stale_query:
                    try:
                        web_result = invoker.invoke(
                            "web_search",
                            {"query": message[:200]},
                            inline_result=True,
                        )
                        if (
                            isinstance(web_result, dict)
                            and web_result.get("success") is True
                        ):
                            web_payload = web_result.get("data")
                            # 网页来源必须并入引用表：这条路径不走 step_loop，
                            # citations 只从 knowledge_search 结果取，网页证据
                            # 的 citations 若不在这里补上，回答正文里的 [n] 编号
                            # 就只有知识库卡片、网页来源在界面上完全不可见
                            # （用户实测：检索成功 8 条，页面上一张网页卡片都没有）。
                            citations.extend(web_result.get("citations") or [])
                            trace(
                                "knowledge_workflow_web_search",
                                {
                                    "query": message,
                                    "trigger": (
                                        "freshness_query" if stale_query else "local_insufficient"
                                    ),
                                    "result_count": web_result.get("result_count"),
                                    "sufficient": (web_payload or {}).get("sufficient"),
                                },
                            )
                        else:
                            # 联网失败不阻塞回答：本地证据照常用，作答里自然少一个来源。
                            trace(
                                "knowledge_workflow_web_search_skipped",
                                {
                                    "query": message,
                                    "reason": "web_search_failed",
                                    "error": (web_result or {}).get("error")
                                    if isinstance(web_result, dict)
                                    else None,
                                },
                            )
                    except Exception as exc:  # noqa: BLE001 - 联网是增益不是依赖
                        trace(
                            "knowledge_workflow_web_search_skipped",
                            {"query": message, "reason": "exception", "error": str(exc)[:200]},
                        )
                else:
                    trace(
                        "knowledge_workflow_web_search_skipped",
                        {"query": message, "reason": "local_sufficient"},
                    )

            # 4) 一次 LLM：证据 + prompt 直接出正文（复用 ResponseGenerator
            #    的 retrieved_knowledge / knowledge_citation_hints 拼装）
            knowledge_payloads: list[dict[str, Any]] = [dict(data)]
            if isinstance(web_payload, dict):
                knowledge_payloads.append(web_payload)
            step_result = StepAgentResult(knowledge_results=knowledge_payloads)
            router_decision = turn_plan_router_decision(
                TurnPlan(
                    decision="answer_only",
                    task_frames=[frame],
                    user_intent=frame.user_intent,
                )
            )
            reply = self._generate_knowledge_workflow_answer(
                message,
                session,
                router_decision,
                step_result,
                model_config,
                persona_prompt,
                memory_context,
                conversation_context,
            )
            trace(
                "knowledge_workflow_completed",
                {
                    "query": message,
                    "chunk_count": search_result.get("chunk_count"),
                    "route_phases": search_result.get("route_phases"),
                    "web_used": isinstance(web_payload, dict),
                    "web_result_count": (
                        web_payload.get("result_count")
                        if isinstance(web_payload, dict)
                        else 0
                    ),
                },
            )
            return TaskExecutionResult(
                task_frame_id=row.task_id,
                status="completed",
                reply_fragment=reply,
                citations=citations,
                evidence_results=[dict(data)],
                task_summary="知识库工作流：代码直调检索后一次作答。",
                action_count=2,
            )
        except Exception as exc:  # noqa: BLE001 - 一律降级标准 Harness
            if self.turn_stream is not None:
                self.turn_stream.reset()
            trace(
                "knowledge_workflow_fallback",
                {"query": message, "reason": str(exc)[:300]},
            )
            return None

    def _generate_knowledge_workflow_answer(
        self,
        message: str,
        session: ChatSession,
        router_decision: Any,
        step_result: StepAgentResult,
        model_config: Any,
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, Any] | None,
    ) -> str:
        """知识工作流的唯一一次作答调用。

        2026-10-19：这条路径用**真流式**（``generate_stream`` + ``sink.delta``）：
        正文跨整个生成过程逐段下发，首字延迟 = TTFT。因为吐字窗口是整轮生成
        （数秒级），不会被传输/渲染层合批成「一下出来」。收尾仍由
        ``_handle_turn_stream_v2`` 的 ``reconcile`` 用最终正文（引用编号可能
        重排）对账。非流式轮次（turn_stream 为 None）回退阻塞 ``generate``。
        """

        arguments = (
            message,
            session,
            None,
            router_decision,
            step_result,
            None,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            None,
        )
        self._emit_stream_status("preparing", "正在整理知识答案")
        stream = self.turn_stream
        # 专用 operation 名（`response.knowledge_answer`）：仅在 output_policy 里对该
        # operation 关掉思考链。通用作答仍走 response.generate(_stream)，不受影响。
        if stream is None:
            return self.owner.response_generator.generate(
                *arguments, operation=KNOWLEDGE_ANSWER_OPERATION
            )
        # 真流式：逐 token 下发；中途失败会把已下发内容作废并抛给调用方降级
        # （见 _run_knowledge_workflow 的 except）。
        parts: list[str] = []
        for chunk in self.owner.response_generator.generate_stream(
            *arguments, operation=KNOWLEDGE_ANSWER_OPERATION
        ):
            parts.append(chunk)
            self._emit_stream_delta(chunk)
        return "".join(parts).strip()

    def _synthesize_reply(
        self,
        request: ChatTurnRequest,
        execution_request: ChatTurnRequest,
        session: ChatSession,
        response_skill: Skill | None,
        router_decision: Any,
        last_step_result: StepAgentResult,
        model_config: Any,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, Any] | None,
        execution_payloads: list[dict[str, object]],
    ) -> str:
        """本轮最终回复的综合生成。

        2026-10-19：不再走真流式预览通道，统一一次性生成，正文由
        ``_handle_turn_stream_v2`` 的 8 字/45ms 匀速回放下发。
        """

        arguments = (
            execution_request.message,
            session,
            response_skill,
            router_decision,
            last_step_result,
            None,
            model_config,
            self.owner._get_persona_prompt(request.tenant_id, session.agent_id),
            memory_context,
            conversation_context,
            execution_payloads,
        )
        # 2026-10-19：不再走真流式预览通道（预览会让正文跟随上游分包抖动），
        # 改为一次性生成，正文由 _handle_turn_stream_v2 的 8 字/45ms 匀速回放。
        self._emit_stream_status("preparing", "正在生成回复")
        return self.owner.response_generator.generate(*arguments)

