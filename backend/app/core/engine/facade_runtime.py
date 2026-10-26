"""AgentLoop 的轮次运行时：会话/模型/人设/上下文/消息落库/记忆捕获/收尾与取消持久化（批次 4.7 自 facade.py 拆出，mixin 化，零行为改动）。"""

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

from app.core.engine.facade_stream import (
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
from app.core.engine.facade_stream import AgentLoopPreconditionError
from app.core.engine.facade_stream import (
    _agent_identity_prompt,
    _find_handoff_node_id_in_skill,
    _knowledge_scope_ids,
    _metadata_prompt_text,
    _single_line_text,
)

logger = logging.getLogger(__name__)


class _TurnRuntimeMixin:
    def _get_or_create_session(self, request: ChatTurnRequest) -> ChatSession:
        session_id = request.session_id or new_id("session")
        chat_session = self.db.get(ChatSession, session_id)
        if not chat_session:
            chat_session = ChatSession(
                id=session_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                channel=(
                    request.channel
                    if request.channel in {PILOTDECK_GROUP_CHAT_CHANNEL, "skill_test"}
                    else None
                ),
            )
            self.db.add(chat_session)
            self.db.flush()
            self._bump_agent_usage(request.agent_id)
        elif not chat_session.agent_id and request.agent_id:
            chat_session.agent_id = request.agent_id
        return chat_session

    def _bump_agent_usage(self, agent_id: str | None) -> None:
        """数字员工「被使用」计数 +1(每新建一个会话算一次,站内与分享访客都计入)。

        公共实现见 `app.agents.usage.bump_agent_usage`（Public API 建会话也走它）。
        """
        bump_agent_usage(self.db, agent_id)

    def _current_skill_step(
        self, skill: Skill, active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.current_step_from_steps(self._skill_steps(skill), active_step_id)

    def _skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        return GraphRules.slot_satisfied(slots, field)

    def _get_request_model(
        self,
        request: ChatTurnRequest,
        agent_id: str | None = None,
        role: str = "default",
    ) -> ModelConfig | None:
        if request.model_config_id:
            row = self.db.get(ModelConfig, request.model_config_id)
            if not row or row.tenant_id != request.tenant_id:
                raise AgentLoopPreconditionError("invalid_model_config", "选中的模型配置不存在。")
            if not row.enabled:
                raise AgentLoopPreconditionError("disabled_model_config", "选中的模型配置已停用。")
            return resolve_model_config_for_runtime(self.db, request.tenant_id, row.id)
        return self._get_default_model(
            request.tenant_id, agent_id, role, user_id=request.user_id
        )

    def _get_default_model(
        self,
        tenant_id: str,
        agent_id: str | None = None,
        role: str = "default",
        user_id: str | None = None,
    ) -> ModelConfig | None:
        # 免费链路:员工被管理员标记 + 用户在该角色上没有自己的模型 → 用租户的全局免费模型
        # (配额在发送入口 chat_stream 已校验并消耗;这里只做解析,所有角色统一落到免费模型)。
        free_candidate = resolve_turn_default_model(self.db, tenant_id, agent_id, user_id, role)
        if free_candidate is not None:
            return resolve_model_config_for_runtime(self.db, tenant_id, free_candidate.id)
        return model_for_agent(self.db, tenant_id, agent_id, role, user_id=user_id)

    def _get_persona_prompt(self, tenant_id: str, agent_id: str | None = None) -> str | None:
        agent = self._get_agent_profile(tenant_id, agent_id)
        if agent and not agent.is_overall:
            return _agent_identity_prompt(agent)
        if agent and agent.is_overall and agent.persona_prompt:
            return agent.persona_prompt
        row = self.db.get(PersonaConfig, tenant_id)
        return row.system_prompt if row else None

    def _get_agent_loop_max_actions(
        self, tenant_id: str, agent_id: str | None = None
    ) -> int:
        if not hasattr(self.db, "get"):
            return MAX_TOOL_ACTIONS_PER_TURN
        agent = self.db.get(AgentProfile, agent_id) if agent_id else None
        if agent is not None and (
            agent.tenant_id != tenant_id or agent.status != "active"
        ):
            agent = None
        if agent is not None:
            value = agent.harness_max_actions
            return max(1, min(int(value), MAX_TOOL_ACTIONS_PER_TURN_LIMIT))
        row = self.db.get(UIConfig, tenant_id)
        value = row.agent_loop_max_actions if row else MAX_TOOL_ACTIONS_PER_TURN
        return max(1, min(int(value), MAX_TOOL_ACTIONS_PER_TURN_LIMIT))

    def _get_conversation_context_settings(
        self,
        tenant_id: str,
    ) -> ConversationContextSettings:
        if not hasattr(self.db, "get"):
            return ConversationContextSettings()
        row = self.db.get(UIConfig, tenant_id)
        if row is None:
            return ConversationContextSettings()
        return ConversationContextSettings(
            token_budget=getattr(row, "context_token_budget", 32_000),
            compaction_trigger_ratio=getattr(
                row,
                "context_compaction_trigger_ratio",
                0.70,
            ),
            recent_round_limit=getattr(row, "context_recent_round_limit", 6),
            long_summary_token_budget=getattr(
                row,
                "context_long_summary_token_budget",
                4_000,
            ),
            medium_summary_token_budget=getattr(
                row,
                "context_medium_summary_token_budget",
                4_000,
            ),
            allowed_roles=frozenset(
                getattr(row, "context_allowed_roles", None)
                or {"user", "assistant"}
            ),
            long_summary_prefix=getattr(
                row,
                "context_long_summary_prefix",
                "历史的信息可以被总结为：",
            ),
            medium_summary_prefix=getattr(
                row,
                "context_medium_summary_prefix",
                "近期的历史信息总结为：",
            ),
        ).normalized()

    def _list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
        return visible_published_skills(self.db, tenant_id, agent_id)

    def _get_agent_profile(self, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
        if not agent_id:
            return None
        row = self.db.get(AgentProfile, agent_id)
        if not row or row.tenant_id != tenant_id or row.status != "active":
            return None
        return row

    def _get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None:
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def _drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        skills_by_id = {skill.skill_id: skill for skill in skills}
        available_skill_ids = set(skills_by_id)
        changed = False
        removed_skill_ids: set[str] = set()
        repaired_steps: list[dict[str, str | None]] = []

        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            changed = True

        def frame_skill_id(frame: object) -> str:
            if not isinstance(frame, dict):
                return ""
            return str(frame.get("target_skill_id") or frame.get("skill_id") or "").strip()

        def keep_frame(frame: object) -> bool:
            skill_id = frame_skill_id(frame)
            if not skill_id:
                return True
            if skill_id in available_skill_ids:
                return True
            removed_skill_ids.add(skill_id)
            return False

        active_skill_id = str(chat_session.active_skill_id or "").strip()
        if active_skill_id and active_skill_id not in available_skill_ids:
            removed_skill_ids.add(active_skill_id)
            chat_session.active_skill_id = None
            chat_session.active_step_id = None
            chat_session.slots_json = {}
            chat_session.awaiting_input_json = None
            chat_session.resume_after_answer_json = None
            changed = True
        elif active_skill_id:
            active_skill = skills_by_id[active_skill_id]
            active_step_id = str(chat_session.active_step_id or "").strip()
            restored_step_id = self._first_step_id(active_skill)
            if (
                restored_step_id
                and active_step_id != restored_step_id
                and not self._skill_has_step(active_skill, active_step_id)
            ):
                chat_session.active_step_id = restored_step_id
                awaiting = (
                    chat_session.awaiting_input_json
                    if isinstance(chat_session.awaiting_input_json, dict)
                    else {}
                )
                task_id = awaiting.get("task_id")
                chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
                chat_session.last_agent_question = None
                repaired_steps.append(
                    {
                        "skill_id": active_skill_id,
                        "from_step_id": active_step_id,
                        "to_step_id": restored_step_id,
                    }
                )
                changed = True

        for attr in ("pending_tasks_json",):
            value = getattr(chat_session, attr) or []
            if not isinstance(value, list):
                continue
            kept = [frame for frame in value if keep_frame(frame)]
            if len(kept) != len(value):
                setattr(chat_session, attr, kept)
                changed = True

        awaiting = chat_session.awaiting_input_json
        if isinstance(awaiting, dict):
            awaiting_skill_id = str(awaiting.get("skill_id") or "").strip()
            if awaiting_skill_id and awaiting_skill_id not in available_skill_ids:
                removed_skill_ids.add(awaiting_skill_id)
                chat_session.awaiting_input_json = None
                changed = True

        if changed:
            chat_session.updated_at = utc_now()
            if hasattr(self, "events"):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_state_pruned",
                    {
                        "removed_skill_ids": sorted(removed_skill_ids),
                        "repaired_steps": repaired_steps,
                    },
                )
        return changed

    def _conversation_context(
        self,
        chat_session: ChatSession,
        model_config: ModelConfig | None = None,
    ) -> dict[str, object]:
        if not hasattr(self, "db") or not hasattr(self.db, "exec"):
            return build_conversation_context([])
        rows = list(
            self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == chat_session.tenant_id,
                    Message.session_id == chat_session.id,
                )
                .order_by(Message.created_at.asc())
            ).all()
        )
        visible_rows = visible_message_rows(rows)
        context = build_conversation_context(
            [
                ConversationProjection.message_context_entry(
                    row,
                    content=visible_message_content(row),
                )
                for row in visible_rows
            ],
            settings=self._get_conversation_context_settings(chat_session.tenant_id),
            context_state=chat_session.context_state_json,
            summary_builder=self._context_summary_builder(model_config) if model_config else None,
        )
        next_state = context.get("context_state")
        if isinstance(next_state, dict) and next_state != (chat_session.context_state_json or {}):
            chat_session.context_state_json = next_state
            self.db.add(chat_session)
        return context

    def _context_summary_builder(self, model_config: ModelConfig) -> Callable[[str, str, int], str]:
        def summarize(label: str, source: str, token_budget: int) -> str:
            payload = stage_payload(
                phase="Context Compression",
                user_message=f"请压缩{label}",
                conversation_context={},
                memory_context=None,
                instructions=(
                    "把输入的历史对话压缩成一段可供后续对话继续使用的中文事实摘要。"
                    "保留用户身份与偏好、已确认事实、未完成任务、关键约束、工具或知识结论；"
                    "删除寒暄、重复内容、内部 ID、时间戳和推理过程，不新增原文没有的信息。"
                ),
                stage_data={"history_to_compress": source},
                output_contract=(f"只输出一段纯文本摘要，控制在约 {token_budget} tokens 以内。"),
            )
            with llm_operation("context.compact"):
                return (
                    LLMClient(model_config).generate_text(unified_system_prompt(), payload).strip()
                )

        return summarize

    def _message_context_entry(self, row: Message) -> dict[str, Any]:
        return ConversationProjection.message_context_entry(row)

    def _assistant_message_metadata(
        self,
        step_result: StepAgentResult | None,
        chat_session: ChatSession,
        source_message: str | None = None,
    ) -> dict[str, Any]:
        return ConversationProjection.assistant_message_metadata(
            step_result, citation_deduper=self._dedupe_knowledge_citations
        )

    def _dedupe_knowledge_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return ConversationProjection.dedupe_knowledge_citations(citations)

    def _append_message(
        self,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        message = Message(
            tenant_id=tenant_id,
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        self.db.add(message)
        return message

    def _persist_cancelled_assistant_message(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        user_message_id: str,
        client_turn_id: str | None = None,
    ) -> Message | None:
        if not user_message_id:
            return None
        user_message = self.db.get(Message, user_message_id)
        if (
            not user_message
            or user_message.tenant_id != tenant_id
            or user_message.session_id != chat_session.id
            or user_message.role != "user"
        ):
            return None

        normalized_client_turn_id = (client_turn_id or "").strip()
        turn_ids = {user_message_id}
        if normalized_client_turn_id:
            turn_ids.add(normalized_client_turn_id)
        existing_messages = self.db.exec(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.session_id == chat_session.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at)
        ).all()
        for row in existing_messages:
            metadata = row.metadata_json or {}
            row_turn_ids = {
                str(metadata.get("turn_id") or "").strip(),
                str(metadata.get("user_message_id") or "").strip(),
                str(metadata.get("client_turn_id") or "").strip(),
            }
            if turn_ids & row_turn_ids:
                return None

        chat_session.updated_at = utc_now()
        chat_session.status = "active"
        chat_session.summary = f"最近回复：{CANCELLED_ASSISTANT_REPLY}"
        user_visibility = str(
            (user_message.metadata_json or {}).get("message_visibility") or "visible"
        )
        cancelled_metadata = {
            "turn_id": user_message_id,
            "user_message_id": user_message_id,
            "client_turn_id": normalized_client_turn_id or None,
            "status": "cancelled",
        }
        if user_visibility != "visible":
            cancelled_metadata["message_visibility"] = user_visibility
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            CANCELLED_ASSISTANT_REPLY,
            metadata=cancelled_metadata,
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            {
                "message_id": assistant_message.id,
                "assistant_message_id": assistant_message.id,
                "user_message_id": user_message_id,
                "turn_id": user_message_id,
                "client_turn_id": normalized_client_turn_id or None,
                "reply": CANCELLED_ASSISTANT_REPLY,
                "status": "cancelled",
            },
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )
        return assistant_message

    def _user_message_metadata(self, request: ChatTurnRequest) -> dict[str, Any]:
        return ConversationProjection.user_message_metadata(request)

    def _finish_with_error(
        self, chat_session: ChatSession, code: str, message: str
    ) -> ChatTurnResponse:
        reply = format_runtime_failure_reply(
            "系统配置错误",
            message,
            code,
            "请在管理端补齐配置后重试。",
        )
        self.events.record(
            chat_session.tenant_id,
            chat_session.id,
            "error_occurred",
            {"code": code, "message": message},
        )
        reply = self._finalize_turn(chat_session, chat_session.tenant_id, reply)
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            session_state=public_session(chat_session),
        )

    def _finalize_turn(
        self,
        chat_session: ChatSession,
        tenant_id: str,
        reply: str,
        step_result: StepAgentResult | None = None,
        source_message: str | None = None,
        user_message_id: str | None = None,
        assistant_metadata_override: dict[str, Any] | None = None,
    ) -> str:
        chat_session.updated_at = utc_now()
        if chat_session.status != "handoff":
            chat_session.status = "active"
        metadata = self._assistant_message_metadata(step_result, chat_session, source_message)
        if assistant_metadata_override:
            metadata = {**metadata, **dict(assistant_metadata_override)}
        reply = restore_truncated_atomic_references(reply, metadata.get("knowledge_citations"))
        reply = self._normalize_reply_citation_labels(reply, metadata.get("knowledge_citations"))
        reply = self._strip_trailing_citation_summary(reply)
        reply, compacted_citations = compact_knowledge_citation_labels(
            reply,
            metadata.get("knowledge_citations"),
        )
        # 租户关掉「展示知识来源」后，正文里的 [1] 也一起清掉：只靠前端裁剪的话，
        # 老客户端 / 缓存里的旧包 / 分享页的第二端都会漏出悬空编号。
        if knowledge_citations_hidden(self.db, tenant_id):
            reply = strip_citation_markers(reply)
        metadata = dict(metadata)
        if compacted_citations:
            metadata["knowledge_citations"] = compacted_citations
        else:
            metadata.pop("knowledge_citations", None)
            metadata.pop("knowledge_query", None)
        if not chat_session.title and source_message:
            fallback_title = self._fallback_session_title_from_message(source_message)
            if fallback_title:
                chat_session.title = fallback_title
        chat_session.summary = f"最近回复：{reply[:120]}"
        assistant_metadata = dict(metadata or {})
        if user_message_id:
            assistant_metadata.setdefault("user_message_id", user_message_id)
            assistant_metadata.setdefault("turn_id", user_message_id)
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            reply,
            metadata=assistant_metadata,
        )
        event_payload: dict[str, Any] = {
            "message_id": assistant_message.id,
            "assistant_message_id": assistant_message.id,
            "reply": reply,
        }
        if user_message_id:
            event_payload["user_message_id"] = user_message_id
            event_payload["turn_id"] = user_message_id
        if assistant_metadata.get("knowledge_citations"):
            event_payload["knowledge_citations"] = assistant_metadata["knowledge_citations"]
        if assistant_metadata.get("message_visibility"):
            event_payload["message_visibility"] = assistant_metadata["message_visibility"]
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            event_payload,
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )
        return reply

    def _mark_session_running(self, chat_session: ChatSession) -> None:
        if chat_session.status == "handoff":
            return
        chat_session.status = "running"
        chat_session.updated_at = utc_now()
        self.db.add(chat_session)

    @staticmethod
    def _fallback_session_title_from_message(message: str) -> str:
        return ConversationProjection.fallback_session_title(message)

    def _normalize_reply_citation_labels(self, reply: str, citations: object) -> str:
        return ConversationProjection.normalize_reply_citation_labels(reply, citations)

    def _strip_trailing_citation_summary(self, reply: str) -> str:
        return ConversationProjection.strip_trailing_citation_summary(reply)
