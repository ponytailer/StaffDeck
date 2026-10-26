"""AgentLoop 的技能图/交接执行：handoff 判定与创建、步骤结果落库、图步排队与激活（批次 4.7 自 facade.py 拆出，mixin 化，零行为改动）。"""

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


class _SkillGraphMixin:
    def _current_step_allows_human_handoff(
        self, skill: Skill | None, active_step_id: str | None
    ) -> bool:
        if not skill:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        return bool(current_step and self._step_declares_human_handoff(current_step))

    def _maybe_route_to_handoff_node(
        self, chat_session: ChatSession, active_skill: Skill | None
    ) -> bool:
        """当 step_result.handoff=True 但当前 step 不声明 handoff 时,
        查找 SOP 中的 handoff 节点并路由到它。这使得后续的
        _create_human_handoff_request 能从 handoff 节点读取 assignee_user_id。

        返回 True 表示已路由到 handoff 节点。
        """
        if not active_skill or not chat_session.active_skill_id:
            return False
        current_step = self._current_skill_step(
            active_skill, chat_session.active_step_id
        )
        if current_step and self._step_declares_human_handoff(current_step):
            return False
        handoff_step_id = _find_handoff_node_id_in_skill(
            active_skill, chat_session.active_step_id
        )
        if not handoff_step_id:
            return False
        self._change_active_step(
            chat_session.tenant_id,
            chat_session,
            handoff_step_id,
            reason="handoff_node_routed_by_step_result",
        )
        return True

    def _step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        node_type = str(step.get("type") or "").strip()
        return node_type == "handoff" or "handoff_human" in self._step_actions(step)

    def _human_handoff_assignee_user_id(
        self, tenant_id: str, agent_id: str | None, fallback_user_id: str | None
    ) -> str | None:
        return HumanHandoffService(self.db, getattr(self, "events", None)).assignee_user_id(
            tenant_id,
            agent_id,
            fallback_user_id,
            tenant_admin_resolver=self._human_handoff_tenant_admin_user_id,
        )

    def _human_handoff_tenant_admin_user_id(self, tenant_id: str) -> str | None:
        return HumanHandoffService(self.db, getattr(self, "events", None)).tenant_admin_user_id(
            tenant_id
        )

    def _human_handoff_context_summary(self, chat_session: ChatSession) -> str:
        return HumanHandoffService(self.db, getattr(self, "events", None)).context_summary(
            chat_session
        )

    def _human_handoff_pending_question(
        self, current_step: dict[str, Any] | None, step_result: StepAgentResult
    ) -> str:
        return HumanHandoffService.pending_question(current_step, step_result)

    def _step_actions(self, step: dict[str, Any]) -> list[str]:
        return GraphRules.step_actions(step)

    def _finish_stale_completed_skill(
        self, tenant_id: str, chat_session: ChatSession, skills: list[Skill]
    ) -> None:
        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            chat_session.updated_at = utc_now()
        active_skill = next(
            (skill for skill in skills if skill.skill_id == chat_session.active_skill_id), None
        )
        if active_skill and self._is_terminal_skill_state(active_skill, chat_session):
            self._complete_active_skill(
                tenant_id, chat_session, active_skill, "stale_terminal_state"
            )

    def _should_complete_skill(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        # Graph topology is authoritative for SOP completion. A non-terminal
        # node may allow an interim reply and all global slots may already be
        # filled, but an outgoing edge still means the workflow has work left.
        # Check this before the reply/tool completion shortcuts so transitioning
        # into an intermediate node cannot finish the entire SOP.
        if self._graph_flow_has_unfinished_work(skill, chat_session, step_result):
            return False
        if (
            tool_result
            and tool_result.success
            and self._current_step_can_finish_after_tool(skill, chat_session)
        ):
            return True
        if self._graph_pending_steps(chat_session):
            return False
        if self._is_answer_ready_skill_state(skill, chat_session):
            return True
        if self._is_terminal_skill_state(skill, chat_session):
            return True
        if not step_result.next_step_id and not step_result.tool_call:
            return True
        return self._is_terminal_skill_state(skill, chat_session)

    def _is_terminal_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._is_terminal_skill_position(
            skill, chat_session.active_step_id, chat_session.slots_json or {}
        )

    def _is_answer_ready_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        required = [str(field) for field in (skill.content_json or {}).get("required_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in required
        )

    def _graph_flow_has_unfinished_work(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult | None = None,
    ) -> bool:
        if not skill or chat_session.active_skill_id != skill.skill_id:
            return False
        if self._graph_pending_steps(chat_session):
            return True
        if (
            step_result
            and step_result.next_step_id
            and str(step_result.next_step_id) == str(chat_session.active_step_id)
        ):
            return True
        if not chat_session.active_step_id:
            return False
        return bool(self._graph_outgoing_edges(skill).get(chat_session.active_step_id))

    def _is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        if not active_step_id:
            return False
        content = skill.content_json or {}
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        return GraphRules.terminal_position_from_step(
            content,
            active_step_id,
            slots,
            self._current_skill_step(skill, active_step_id),
            self._skill_slot_satisfied,
            self._step_actions,
        )

    def _current_step_can_finish_after_tool(self, skill: Skill, chat_session: ChatSession) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
        return GraphRules.actions_allow_final_reply(actions)

    def _complete_active_skill(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill, reason: str
    ) -> None:
        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.complete_current_skill(chat_session)
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_completed",
            {
                "skill_id": before_skill or skill.skill_id,
                "step_id": before_step,
                "reason": reason,
                "resumed_skill_id": chat_session.active_skill_id,
                "resumed_step_id": chat_session.active_step_id,
            },
        )

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        return TurnFinalizer.finalize(
            tenant_id,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            current_step_allows_handoff=self._current_step_allows_human_handoff,
            route_to_handoff_node=self._maybe_route_to_handoff_node,
            create_handoff=self._create_human_handoff_request,
            record_event=self.events.record,
            should_complete=self._should_complete_skill,
            complete_skill=self._complete_active_skill,
        )

    def _create_human_handoff_request(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
    ) -> HumanHandoffRequest:
        # SOP 节点指定的处理人:从当前 step 的 assignee_user_id 字段读取
        # (handoff 类型节点或 allowed_actions 含 handoff_human 的节点可配置)。
        # 渠道功能已移除,通知统一走网页收件箱。
        step_assignee_user_id: str | None = None
        current_step = (
            self._current_skill_step(active_skill, chat_session.active_step_id)
            if active_skill
            else None
        )
        if isinstance(current_step, dict):
            step_assignee_user_id = (
                str(current_step.get("assignee_user_id") or "").strip() or None
            )
        return HumanHandoffService(self.db, self.events).create(
            tenant_id,
            chat_session,
            step_result,
            current_step_resolver=lambda: current_step,
            assignee_resolver=self._human_handoff_assignee_user_id,
            context_summary=self._human_handoff_context_summary,
            pending_question=self._human_handoff_pending_question,
            step_assignee_user_id=step_assignee_user_id,
        )

    def _apply_step_result(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None = None,
    ) -> None:
        source_skill_id = chat_session.active_skill_id
        source_step_id = chat_session.active_step_id
        if step_result.slot_updates:
            chat_session.slots_json = {
                **(chat_session.slots_json or {}),
                **step_result.slot_updates,
            }
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        active_skill_matches = bool(
            active_skill and active_skill.skill_id == chat_session.active_skill_id
        )
        invalid_next_step = False
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            if not self._skill_has_step(active_skill, next_step_id):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "step_agent_result_repaired",
                    {
                        "mode": "invalid_next_step_ignored",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "invalid_next_step_id": step_result.next_step_id,
                    },
                )
                step_result.next_step_id = None
                step_result.is_step_completed = False
                invalid_next_step = True

        self._sync_awaiting_input_from_step_result(
            chat_session,
            step_result,
            active_skill,
            source_skill_id=source_skill_id,
            source_step_id=source_step_id,
        )

        if not chat_session.active_skill_id:
            return
        if invalid_next_step:
            return
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            source_step_id = chat_session.active_step_id
            pending_steps = self._graph_pending_steps(chat_session)
            if pending_steps:
                if next_step_id in pending_steps:
                    pending_steps = [item for item in pending_steps if item != next_step_id]
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                    self._change_active_step(
                        tenant_id,
                        chat_session,
                        next_step_id,
                        reason="graph_merge_step",
                    )
                    return

                if next_step_id not in pending_steps:
                    pending_steps.append(next_step_id)
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                if self._activate_next_pending_graph_step(
                    tenant_id,
                    chat_session,
                    active_skill,
                    reason="graph_sibling_step",
                ):
                    step_result.next_step_id = chat_session.active_step_id
                return

            self._queue_graph_sibling_steps(
                tenant_id,
                chat_session,
                active_skill,
                source_step_id,
                next_step_id,
            )

        if step_result.next_step_id:
            self._change_active_step(tenant_id, chat_session, str(step_result.next_step_id).strip())
            return

        if active_skill_matches and step_result.is_step_completed:
            if self._activate_next_pending_graph_step(
                tenant_id,
                chat_session,
                active_skill,
                reason="graph_pending_step",
            ):
                step_result.next_step_id = chat_session.active_step_id

    def _sync_awaiting_input_from_step_result(
        self,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None,
        *,
        source_skill_id: str | None,
        source_step_id: str | None,
    ) -> None:
        if not active_skill or active_skill.skill_id != source_skill_id or not source_step_id:
            return

        step = self._current_skill_step(active_skill, source_step_id)
        if not step:
            return
        missing_fields = [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._skill_slot_satisfied(chat_session.slots_json or {}, str(field))
        ]
        is_waiting_reply = step_result.action in {"ask_user", "clarify"}
        if is_waiting_reply and missing_fields:
            previous = (
                chat_session.awaiting_input_json
                if isinstance(chat_session.awaiting_input_json, dict)
                else {}
            )
            awaiting_input = {
                "skill_id": source_skill_id,
                "step_id": source_step_id,
                "expected_fields": missing_fields,
                "question_summary": str(step_result.reply or "").strip() or None,
            }
            if previous.get("task_id"):
                awaiting_input["task_id"] = previous["task_id"]
            chat_session.awaiting_input_json = awaiting_input
            chat_session.last_agent_question = awaiting_input["question_summary"]
            return

        should_clear = bool(
            step_result.next_step_id
            or step_result.tool_call
            or step_result.is_step_completed
            or not missing_fields
        )
        awaiting = chat_session.awaiting_input_json
        if not should_clear or not isinstance(awaiting, dict):
            return
        if awaiting.get("skill_id") not in {None, source_skill_id}:
            return
        if awaiting.get("step_id") not in {None, source_step_id}:
            return
        task_id = awaiting.get("task_id")
        chat_session.awaiting_input_json = {"task_id": task_id} if task_id else None
        chat_session.last_agent_question = None

    def _change_active_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        next_step_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        if previous_step == next_step_id:
            return
        payload: dict[str, Any] = {
            "from_skill_id": chat_session.active_skill_id,
            "to_skill_id": chat_session.active_skill_id,
            "from_step_id": previous_step,
            "to_step_id": next_step_id,
        }
        if reason:
            payload["reason"] = reason
        self.events.record(tenant_id, chat_session.id, "skill_step_changed", payload)

    def _graph_pending_steps(self, chat_session: ChatSession) -> list[str]:
        value = (chat_session.slots_json or {}).get(GRAPH_PENDING_STEPS_SLOT)
        return GraphRules.normalize_pending_steps(value)

    def _store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        slots = dict(chat_session.slots_json or {})
        normalized = GraphRules.normalize_pending_steps(pending_steps)
        if normalized:
            slots[GRAPH_PENDING_STEPS_SLOT] = normalized
        else:
            slots.pop(GRAPH_PENDING_STEPS_SLOT, None)
        chat_session.slots_json = slots
        self.events.record(
            tenant_id,
            chat_session.id,
            "graph_pending_steps_updated",
            {"pending_step_ids": normalized},
        )

    def _queue_graph_sibling_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        source_step_id: str | None,
        selected_step_id: str,
    ) -> None:
        if not source_step_id:
            return
        outgoing = self._graph_outgoing_edges(active_skill).get(source_step_id) or []
        sibling_steps = GraphRules.sibling_steps_from_edges(
            outgoing,
            selected_step_id,
            self._edge_condition,
        )
        if not sibling_steps:
            return
        pending_steps = self._graph_pending_steps(chat_session)
        for step_id in sibling_steps:
            if step_id not in pending_steps:
                pending_steps.append(step_id)
        self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def _edge_condition(self, edge: dict[str, Any]) -> str:
        return GraphRules.edge_condition(edge)

    def _activate_next_pending_graph_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        *,
        reason: str,
    ) -> bool:
        pending_steps = self._graph_pending_steps(chat_session)
        while pending_steps:
            next_step_id = pending_steps.pop(0)
            if not self._skill_has_step(active_skill, next_step_id):
                continue
            self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
            self._change_active_step(tenant_id, chat_session, next_step_id, reason=reason)
            return True
        self._store_graph_pending_steps(tenant_id, chat_session, [])
        return False

    def _skill_has_step(self, skill: Skill, step_id: str | None) -> bool:
        return GraphRules.has_step(skill.content_json or {}, step_id)

    def _first_step_id(self, skill: Skill) -> str | None:
        content = skill.content_json or {}
        start_node_id = str(content.get("start_node_id") or "").strip()
        if start_node_id and self._skill_has_step(skill, start_node_id):
            return start_node_id
        steps = self._skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.steps_from_nodes(self._ordered_skill_nodes(skill))

    def _skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        return GraphRules.nodes(skill.content_json or {})

    def _ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        content = skill.content_json or {}
        return GraphRules.ordered_nodes(
            content,
            nodes=self._skill_nodes(skill),
            outgoing=self._graph_outgoing_edges(skill),
        )

    def _graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        return GraphRules.outgoing_edges(skill.content_json or {})

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        return GraphRules.default_next_step_from_parts(
            self._skill_nodes(skill),
            self._graph_outgoing_edges(skill).get(active_step_id, []),
        )
