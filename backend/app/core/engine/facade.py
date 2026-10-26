from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from time import sleep
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    model_for_agent,
    visible_published_skills,
    visible_skill,
)
from app.agents.free_model import resolve_turn_default_model
from app.agents.usage import bump_agent_usage
from app.chat_pubsub import publish_relay_wake
from app.core.converse.identity import AgentIdentityPrompt
from app.core.base.cancellation import clear_chat_turn_cancelled
from app.core.base.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)
from app.core.base.conversation_projection import ConversationProjection
from app.core.base.graph_rules import GraphRules
from app.core.engine.step_loop import HarnessExecutionCancelled
from app.core.engine.session import HarnessSessionBusy
from app.core.engine.session import HarnessTurnConflict
from app.core.engine.turn import (
    HarnessV2Engine,
    _with_recoverable_first_session,
    get_or_create_harness_session,
)
from app.core.engine.handoff import HumanHandoffService
from app.core.converse.response import (
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.engine.skill_runtime import SkillRuntime
from app.core.plan.slash import SlashCommandError
from app.core.engine.finalizer import TurnFinalizer
from app.core.base.ui_flags import knowledge_citations_hidden
from app.db.models import (
    AgentProfile,
    ChatSession,
    HarnessTurnRecord,
    HumanHandoffRequest,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    UIConfig,
    new_id,
    utc_now,
)
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    restore_truncated_atomic_references,
    strip_citation_markers,
)
from app.config import get_settings
from app.core.knowledge_flow.prefetch import is_smalltalk
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    resolve_model_config_for_runtime,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService
from app.observability import EventLog
from app.observability.spans import llm_operation
from app.session.helpers import public_session
from app.session.message_visibility import visible_message_content, visible_message_rows
from app.session.origin import PILOTDECK_GROUP_CHAT_CHANNEL
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    RouterDecision,
    StepAgentResult,
)
from app.tools.tool_schema import ToolResult

logger = logging.getLogger(__name__)

# I5（qa-optimization-plan 第二批）：显式记忆请求关键词——命中即永不跳过
# memory.capture，即使本轮没有任何槽位/结构化写入。
_MEMORY_CAPTURE_EXPLICIT_KEYWORDS = ("记住", "记一下", "别忘了")


def _memory_capture_skip_reason(
    message: str,
    step_result: StepAgentResult,
) -> str | None:
    """判定本轮 memory.capture 是否值得入队（I5）。

    跳过两类：寒暄（is_smalltalk 词典）；「无槽位写入、无 handoff、无结构化
    结果」的纯问答轮。显式记忆请求（「记住这个」）永不跳过。返回跳过原因，
    None 表示照常入队。
    """

    settings = get_settings()
    if not settings.memory_capture_skip_uninformative_enabled:
        return None
    text = str(message or "").strip()
    if any(keyword in text for keyword in _MEMORY_CAPTURE_EXPLICIT_KEYWORDS):
        return None
    if is_smalltalk(text):
        return "smalltalk"
    has_state_write = (
        bool(step_result.slot_updates)
        or bool(step_result.handoff)
        or step_result.structured_result is not None
    )
    if not has_state_write:
        return "uninformative_turn"
    return None


from app.core.engine.facade_stream import AgentLoopPreconditionError  # noqa: F401  (re-export)
from app.core.engine.facade_graph import _SkillGraphMixin
from app.core.engine.facade_runtime import _TurnRuntimeMixin
from app.core.engine.facade_stream import (  # noqa: F401  (re-export)
    TurnStreamSink,
    _agent_identity_prompt,
    _find_handoff_node_id_in_skill,
    _knowledge_scope_ids,
    _metadata_prompt_text,
    _single_line_text,
)
from app.core.engine.facade_stream import (  # noqa: F401
    CANCELLED_ASSISTANT_REPLY,
    GRAPH_PENDING_STEPS_SLOT,
    MAX_TOOL_ACTIONS_PER_TURN,
    MAX_TOOL_ACTIONS_PER_TURN_LIMIT,
    STREAM_CHUNK_INTERVAL_SECONDS,
    STREAM_CHUNK_SIZE,
    STREAM_DELTA_FLUSH_INTERVAL_SECONDS,
    STREAM_DELTA_FLUSH_MAX_CHARS,
    STREAM_DELTA_MAX_FLUSHES,
    STREAM_TYPING_BUDGET_SECONDS,
)


class AgentLoop(_TurnRuntimeMixin, _SkillGraphMixin):
    def __init__(
        self,
        db: Session,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.events = EventLog(db, event_sink=event_sink)
        self.runtime = SkillRuntime()
        self.response_generator = ResponseGenerator()
        self.memory = MemoryService(db)
        # 仅流式轮次挂载（见 _handle_turn_stream_v2）；非流式路径恒为 None。
        self.turn_stream: TurnStreamSink | None = None

    def _turn_payload(self, payload: dict[str, Any], user_message_id: str | None) -> dict[str, Any]:
        data = dict(payload)
        if user_message_id:
            data.setdefault("user_message_id", user_message_id)
            data.setdefault("turn_id", user_message_id)
        return data

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        engine = HarnessV2Engine(self, turn_stream=self.turn_stream)
        chat_session: ChatSession | None = None
        user_message_id: str | None = None
        step_result = StepAgentResult(action="reply")
        try:
            return engine.run(request)
        except (HarnessTurnConflict, HarnessSessionBusy) as exc:
            chat_session = engine.session
            self.db.rollback()
            chat_session = chat_session or self._get_or_create_session(request)
            error_code = (
                "HARNESS_SESSION_BUSY"
                if isinstance(exc, HarnessSessionBusy)
                else "HARNESS_TURN_CONFLICT"
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "turn_rejected",
                {
                    "code": error_code,
                    "message": str(exc),
                    "client_turn_id": request.client_turn_id,
                },
            )
            self.db.commit()
            return ChatTurnResponse(
                reply=format_runtime_failure_reply(
                    "Harness 并发或重复请求已阻止",
                    exc,
                    error_code,
                    "请等待原请求完成，或为新请求使用新的 client_turn_id。",
                ),
                session_id=chat_session.id,
                runtime_error_code=error_code,
                step_result=step_result,
                session_state=public_session(chat_session),
            )
        except HarnessExecutionCancelled:
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_cancelled()
            chat_session = chat_session or self._get_or_create_session(request)
            if user_message_id:
                self._persist_cancelled_assistant_message(
                    request.tenant_id,
                    chat_session,
                    user_message_id,
                    request.client_turn_id,
                )
            self.db.commit()
            for turn_id in (user_message_id, request.client_turn_id):
                if turn_id:
                    clear_chat_turn_cancelled(chat_session.id, turn_id)
            return ChatTurnResponse(
                reply=CANCELLED_ASSISTANT_REPLY,
                session_id=chat_session.id,
                step_result=step_result,
                session_state=public_session(chat_session),
            )
        except (AgentLoopPreconditionError, SlashCommandError) as exc:
            chat_session = engine.session
            engine.mark_interrupted(exc.code, exc.message)
            chat_session = chat_session or self._get_or_create_session(request)
            return self._finish_with_error(chat_session, exc.code, exc.message)
        except LLMError as exc:
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_interrupted("LLM_ERROR", str(exc))
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "LLM_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc)
            )
        except Exception as exc:
            chat_session = engine.session
            user_message_id = engine.user_message_id
            engine.mark_interrupted("HARNESS_V2_ERROR", str(exc))
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "HARNESS_V2_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "Harness v2 执行出错",
                exc,
                "HARNESS_V2_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )
        finally:
            terminal_record = getattr(engine, "turn_record", None)
            terminal_session = getattr(engine, "session", None)
            if (
                terminal_record is not None
                and terminal_session is not None
                and terminal_record.status in {"completed", "failed", "cancelled"}
            ):
                for turn_id in (engine.user_message_id, request.client_turn_id):
                    if turn_id:
                        clear_chat_turn_cancelled(terminal_session.id, turn_id)
            engine.close()

        reply = self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
            assistant_metadata_override=(
                {"message_visibility": request.message_visibility}
                if request.message_visibility != "visible"
                else None
            ),
        )
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            step_result=step_result,
            session_state=public_session(chat_session),
        )

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        yield from self._handle_turn_stream_v2(request)

    def _handle_turn_stream_v2(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        session_request = _with_recoverable_first_session(request)
        existing_session = (
            self.db.get(ChatSession, session_request.session_id)
            if session_request.session_id
            else None
        )
        chat_session = get_or_create_harness_session(
            self,
            session_request,
        )
        created_session = existing_session is None
        scoped_request = request.model_copy(update={"session_id": chat_session.id})
        initial_turn_id = str(request.client_turn_id or "").strip() or None
        if created_session:
            yield self._stream_event(
                "session_created",
                chat_session,
                {
                    "sessionId": chat_session.id,
                    "turn_id": initial_turn_id,
                    "client_turn_id": request.client_turn_id,
                    "execution_engine": "harness_v2",
                },
            )
        yield self._stream_event(
            "user_message_received",
            chat_session,
            self._turn_payload(
                {
                    "sessionId": chat_session.id,
                    "client_turn_id": request.client_turn_id,
                    "execution_engine": "harness_v2",
                },
                initial_turn_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "planning",
            "正在规划本轮任务",
            {"execution_engine": "harness_v2"},
            user_message_id=initial_turn_id,
        )
        # 真流式：把 sink 挂到本轮 AgentLoop 上，harness_v2 在执行期（检索/调工具）
        # 与生成期（最终回复）都能把内容实时写给 SSE relay，而不是等整轮跑完。
        sink = TurnStreamSink(self)
        sink.bind(chat_session.tenant_id, chat_session.id)
        self.turn_stream = sink
        try:
            response = self.handle_turn(scoped_request)
        finally:
            self.turn_stream = None
        sink.flush()
        chat_session = self.db.get(ChatSession, response.session_id)
        if chat_session is None:
            return
        user_message = None
        client_turn_id = str(request.client_turn_id or "").strip()
        if client_turn_id:
            receipt = self.db.exec(
                select(HarnessTurnRecord).where(
                    HarnessTurnRecord.tenant_id == request.tenant_id,
                    HarnessTurnRecord.session_id == response.session_id,
                    HarnessTurnRecord.client_turn_id == client_turn_id,
                )
            ).first()
            if receipt is not None and receipt.user_message_id:
                candidate = self.db.get(Message, receipt.user_message_id)
                if (
                    candidate is not None
                    and candidate.tenant_id == request.tenant_id
                    and candidate.session_id == response.session_id
                    and candidate.role == "user"
                ):
                    user_message = candidate
        if user_message is None and not client_turn_id:
            user_message = self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == request.tenant_id,
                    Message.session_id == response.session_id,
                    Message.role == "user",
                )
                .order_by(Message.created_at.desc())
            ).first()
        user_message_id = user_message.id if user_message else None
        if response.reply == CANCELLED_ASSISTANT_REPLY:
            yield self._stream_event(
                "stream_cancelled",
                chat_session,
                self._turn_payload(
                    {
                        "phase": "cancelled",
                        "text": CANCELLED_ASSISTANT_REPLY,
                        "client_turn_id": request.client_turn_id,
                        "execution_engine": "harness_v2",
                    },
                    user_message_id or initial_turn_id,
                ),
            )
            return
        resolved_turn_id = user_message_id or initial_turn_id
        if response.runtime_error_code:
            yield self._stream_event(
                "error",
                chat_session,
                self._turn_payload(
                    {
                        "code": response.runtime_error_code,
                        "message": response.reply,
                        "client_turn_id": request.client_turn_id,
                        "execution_engine": "harness_v2",
                    },
                    resolved_turn_id,
                ),
            )
            return
        if sink.streamed:
            # 生成期已经真流式下发过正文，这里只做一次对账：最终正文与已下发
            # 内容不一致时原地替换。（当前预览通道已关闭，此分支为防御保留。）
            sink.reconcile(response.reply)
        else:
            # 2026-10-19：回到最初的分片下发，不做节奏控制。后端匀速回放（
            # 8 字/45ms）在前端看起来仍是一次性出现（传输/渲染又把分片合批了），
            # 但那一步会白白多花最多 STREAM_TYPING_BUDGET_SECONDS(2.5s)，故去掉
            # sleep/interval，只保留 8 字分片。
            for chunk in self.response_generator.chunk_text(
                response.reply, chunk_size=STREAM_CHUNK_SIZE
            ):
                event = self._stream_event(
                    "stream_delta",
                    chat_session,
                    self._turn_payload(
                        {
                            "content": chunk,
                            "execution_engine": "harness_v2",
                        },
                        resolved_turn_id,
                    ),
                )
                self.db.commit()
                yield event
        end_event = self._stream_event(
            "stream_end",
            chat_session,
            self._turn_payload({"execution_engine": "harness_v2"}, resolved_turn_id),
        )
        self.db.commit()
        yield end_event
        yield self._stream_event(
            "complete",
            chat_session,
            self._turn_payload(
                {
                    **response.model_dump(mode="json"),
                    "execution_engine": "harness_v2",
                },
                resolved_turn_id,
            ),
        )

    def _stream_status(
        self,
        chat_session: ChatSession,
        phase: str,
        text: str,
        extra: dict[str, object] | None = None,
        user_message_id: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"phase": phase, "text": text, **(extra or {})}
        if user_message_id:
            payload = self._turn_payload(payload, user_message_id)
            if phase != "received":
                self.events.record(
                    chat_session.tenant_id, chat_session.id, "stream_status", payload
                )
                self.db.commit()
        return self._stream_event(
            "status",
            chat_session,
            payload,
        )

    def _stream_event(
        self,
        kind: str,
        chat_session: ChatSession,
        payload: dict[str, object],
    ) -> dict[str, object]:
        persisted_stream_events = {
            "agent_loop_completed",
            "agent_loop_continued",
            "general_skill_run_finished",
            "general_skill_trace",
            "knowledge_result",
            "reflection_decision",
            "skill_state",
            "step_result",
            "stream_delta",
            "stream_replace",
            "stream_end",
            "tool_result",
        }
        if kind in persisted_stream_events and (
            payload.get("turn_id") or payload.get("user_message_id")
        ):
            self.events.record(chat_session.tenant_id, chat_session.id, kind, payload)
            self.db.commit()
        data = {
            "kind": kind,
            "sessionId": chat_session.id,
            "timestamp": utc_now().isoformat(),
            "provider": "skill",
            **payload,
        }
        return {"event": kind, "data": data}

    def _pace_stream(self) -> None:
        sleep(STREAM_CHUNK_INTERVAL_SECONDS)


    def _enqueue_memory_capture(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
    ) -> list[dict[str, object]]:
        skip_reason = _memory_capture_skip_reason(
            request.message, step_result
        )
        if skip_reason:
            # I5：无信息轮不入队 memory.capture，事件留痕供漏答归因与统计。
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_capture_skipped",
                {"reason": skip_reason},
            )
            return []
        try:
            job = enqueue_memory_capture(
                request,
                chat_session.id,
                step_result,
                tool_result,
                model_config.id,
            )
        except Exception as exc:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_error",
                {"message": str(exc)},
            )
            return []
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "async_job_enqueued",
            {"job_id": job.id, "job_name": job.name, "feature": "memory"},
        )
        self.db.commit()
        return [{"job_id": job.id, "job_name": job.name}]

