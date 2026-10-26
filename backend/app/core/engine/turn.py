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


from app.core.engine.turn_frames import _FrameExecutionMixin
from app.core.engine.turn_knowledge import _KnowledgeWorkflowMixin
from app.core.engine.turn_run import _TurnRunMixin

logger = logging.getLogger(__name__)


class HarnessV2Engine(_TurnRunMixin, _FrameExecutionMixin, _KnowledgeWorkflowMixin):
    """Outer planner + durable TaskFrame scheduler + isolated Harness runs."""


    def __init__(self, owner: Any, *, turn_stream: Any | None = None) -> None:
        self.owner = owner
        self.db = owner.db
        self.events = owner.events
        # 流式轮次的实时投递器（AgentLoop.TurnStreamSink）；非流式路径恒为 None。
        self.turn_stream = turn_stream
        self.planner = TurnPlanner()
        self.compiler = TaskRequestCompiler()
        self.manifests = CapabilityManifestBuilder(self.db)
        self.task_agent = HarnessTaskAgent()
        self.store = TaskFrameStore(self.db)
        self.turn_store = HarnessTurnStore(self.db)
        self.session_leases = HarnessSessionLeaseStore(self.db)
        self.turn_record: HarnessTurnRecord | None = None
        self.session_lease: HarnessSessionLeaseToken | None = None
        self.user_message_id: str | None = None
        self.current_source_turn_id: str | None = None
        self.session: ChatSession | None = None
        self.active_frame_id: str | None = None
        self.active_frame_lease_owner: str | None = None
        self.active_frame_attempt_no: int | None = None
        self.active_run_id: str | None = None
        self.slash_command: SlashCommandSelection | None = None
        self._session_lock: Any | None = None
        self._session_lock_id: str | None = None

    def _emit_stream_status(self, phase: str, text: str, **extra: Any) -> None:
        stream = self.turn_stream
        if stream is None:
            return
        stream.status(phase, text, **extra)

    def _emit_stream_delta(self, chunk: str) -> None:
        stream = self.turn_stream
        if stream is None or not chunk:
            return
        stream.delta(chunk)

    def _trace_stream_status(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """把 harness trace 事件翻译成等待期可见的状态（流式轮次才生效）。"""

        if self.turn_stream is None:
            return
        status = _stream_status_for_trace(event_type, payload)
        if status is None:
            return
        phase = str(status.pop("phase"))
        text = str(status.pop("text"))
        self._emit_stream_status(phase, text, **status)

    def _get_or_create_session(
        self,
        request: ChatTurnRequest,
    ) -> ChatSession:
        return get_or_create_harness_session(self.owner, request)

    def _renew_session_lease(self) -> None:
        self.session_leases.renew(self.session_lease)
        self.turn_store.renew(self.turn_record)
        self.db.commit()

    def _renew_execution_leases(
        self,
        row: HarnessTaskFrameRecord,
    ) -> None:
        lease_owner = str(self.active_frame_lease_owner or "")
        attempt_no = self.active_frame_attempt_no
        if not lease_owner or attempt_no is None:
            raise HarnessExecutionFenced(
                "Harness TaskFrame lease token is missing."
            )
        try:
            self.session_leases.renew(self.session_lease)
            self.turn_store.renew(self.turn_record)
            self.store.renew_running_lease(
                row,
                lease_owner=lease_owner,
                attempt_no=attempt_no,
            )
        except (HarnessSessionLeaseLost, TaskFrameClaimConflict) as exc:
            raise HarnessExecutionFenced(str(exc)) from exc
        self.db.commit()

    def mark_cancelled(self) -> None:
        """Close the durable attempt after cooperative stream cancellation."""

        self.db.rollback()
        source_turn_id = str(self.current_source_turn_id or "").strip()
        if self.session is not None and source_turn_id:
            self.store.cancel_source_turn(self.session, source_turn_id)
        else:
            if self.active_run_id:
                run = self.db.get(HarnessRunRecord, self.active_run_id)
                if run is not None and run.status == "running":
                    self.store.finish_run(
                        run,
                        status="cancelled",
                        action_count=run.action_count,
                        result={"status": "cancelled"},
                    )
            if self.active_frame_id:
                row = self.db.get(HarnessTaskFrameRecord, self.active_frame_id)
                if (
                    row is not None
                    and row.status == "running"
                    and row.lease_owner == self.active_frame_lease_owner
                    and row.attempt_no == self.active_frame_attempt_no
                ):
                    self.store.finish_frame(
                        row,
                        status="cancelled",
                        step_id=row.step_id,
                        slots=dict(row.slots_json or {}),
                        result={
                            "task_frame_id": row.task_id,
                            "status": "cancelled",
                            "task_summary": "用户取消了当前 Harness 执行。",
                        },
                    )
                    self.store.finish_agent_loop_for_frame(
                        row,
                        result_status="cancelled",
                        checkpoint=self.store.agent_loop_checkpoint(row),
                        last_run_id=self.active_run_id,
                    )
        self.db.commit()
        turn_store = getattr(self, "turn_store", None)
        if turn_store is not None:
            turn_store.finish_with_error(
                getattr(self, "turn_record", None),
                status="cancelled",
                code="CANCELLED",
                message="用户取消了当前 Harness 执行。",
            )
        self.active_run_id = None
        self.active_frame_id = None
        self.active_frame_lease_owner = None
        self.active_frame_attempt_no = None

    def mark_interrupted(self, code: str, message: str) -> None:
        """Fail the attempt and requeue its frame after an unexpected crash."""

        self.db.rollback()
        source_turn_id = str(self.current_source_turn_id or "").strip()
        if (
            self.session is not None
            and source_turn_id
            and self.active_frame_lease_owner
            and self.active_frame_attempt_no is not None
        ):
            self.store.finish_source_turn_running_runs(
                self.session.id,
                source_turn_id,
                status="failed",
                result={
                    "status": "failed",
                    "error": {"code": code, "message": message},
                },
                lease_owner=self.active_frame_lease_owner,
                attempt_no=self.active_frame_attempt_no,
            )
        elif self.active_run_id:
            run = self.db.get(HarnessRunRecord, self.active_run_id)
            if run is not None and run.status == "running":
                self.store.finish_run(
                    run,
                    status="failed",
                    action_count=run.action_count,
                    result={
                        "status": "failed",
                        "error": {"code": code, "message": message},
                    },
                )
        if self.active_frame_id:
            row = self.db.get(HarnessTaskFrameRecord, self.active_frame_id)
            if (
                row is not None
                and row.status == "running"
                and row.lease_owner == self.active_frame_lease_owner
                and row.attempt_no == self.active_frame_attempt_no
            ):
                self.store.finish_frame(
                    row,
                    status="queued",
                    step_id=row.step_id,
                    slots=dict(row.slots_json or {}),
                    result={
                        "task_frame_id": row.task_id,
                        "status": "failed",
                        "error": {"code": code, "message": message},
                        "task_summary": "Harness 执行中断，TaskFrame 已重新排队。",
                    },
                )
                self.store.finish_agent_loop_for_frame(
                    row,
                    result_status="action_budget",
                    checkpoint=self.store.agent_loop_checkpoint(row),
                    last_run_id=self.active_run_id,
                )
        self.db.commit()
        turn_store = getattr(self, "turn_store", None)
        if turn_store is not None:
            turn_store.finish_with_error(
                getattr(self, "turn_record", None),
                status="failed",
                code=code,
                message=message,
            )
        self.active_run_id = None
        self.active_frame_id = None
        self.active_frame_lease_owner = None
        self.active_frame_attempt_no = None

    def _is_cancelled(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
    ) -> bool:
        user_message_id = str(self.user_message_id or "").strip()
        client_turn_id = str(request.client_turn_id or "").strip()
        return bool(
            user_message_id
            and is_chat_turn_cancelled(
                session.id,
                user_message_id,
                db=self.db,
                identity_kind="message",
            )
        ) or bool(
            client_turn_id
            and is_chat_turn_cancelled(
                session.id,
                client_turn_id,
                db=self.db,
                identity_kind="client",
            )
        )

    def _raise_if_cancelled(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
    ) -> None:
        if self._is_cancelled(request, session):
            raise HarnessExecutionCancelled(
                "Harness execution was cancelled by the user."
            )

    def _activate_frame(
        self,
        session: ChatSession,
        row: HarnessTaskFrameRecord,
        skills: list[Skill],
    ) -> Skill | None:
        if row.kind != "sop":
            return None
        active_skill = next(
            (skill for skill in skills if skill.skill_id == row.skill_id),
            None,
        )
        if active_skill is None:
            return None
        self._record_skill_activation_event(session, row, active_skill)
        self.owner.runtime.restore_task_frame(
            session,
            {
                "task_id": row.task_id,
                "skill_id": row.skill_id,
                "step_id": row.step_id,
                "slots": dict(row.slots_json or {}),
                "awaiting_input": {},
            },
        )
        return active_skill

    def _record_skill_activation_event(
        self,
        session: ChatSession,
        row: HarnessTaskFrameRecord,
        skill: Skill,
    ) -> None:
        """新发起/恢复 SOP 的 TaskFrame 落一条运行时事件。

        SOP 调用次数统计(api/skills._skill_stats)只认 skill_started /
        skill_resumed 事件;legacy 运行时移除后这两个事件不再产生,导致
        管理端调用次数永远是 0。这里在新任务(start_new_task)与恢复
        挂起任务(switch_to_pending)被调度时补记,payload 保持旧结构
        (to_skill_id/to_skill_version/from_skill_id/from_step_id),
        继续执行的 continue_active 不计新调用。
        """
        if row.decision == "start_new_task":
            event_type = "skill_started"
        elif row.decision == "switch_to_pending":
            event_type = "skill_resumed"
        else:
            return
        payload = {
            "decision": row.decision,
            "from_skill_id": session.active_skill_id,
            "to_skill_id": skill.skill_id,
            "from_skill_version": None,
            "to_skill_version": skill.version,
            "from_step_id": session.active_step_id,
            "to_step_id": row.step_id,
            "execution_engine": "harness_v2",
            "task_frame_id": row.task_id,
        }
        self.events.record(
            session.tenant_id,
            session.id,
            event_type,
            payload,
        )

    def _restore_visible_active_frame(
        self,
        session: ChatSession,
        records: list[HarnessTaskFrameRecord],
        pre_turn_state: dict[str, Any],
    ) -> None:
        if session.status == "handoff":
            handoff_row = next(
                (row for row in records if row.status == "handoff"),
                None,
            )
            if handoff_row is not None:
                self.store.set_active_task_frame(session, handoff_row)
            return
        awaiting_conversation = self.store.latest_awaiting_conversation(
            session
        )
        if awaiting_conversation is not None:
            result = awaiting_conversation.result_json or {}
            reply = str(result.get("reply_fragment") or "").strip()
            requirement = awaiting_conversation.task_requirement_json or {}
            session.active_skill_id = None
            session.active_step_id = None
            session.slots_json = {}
            session.resume_after_answer_json = None
            session.awaiting_input_json = {
                "task_id": awaiting_conversation.task_id,
                "kind": "conversation",
                "expected_fields": list(
                    requirement.get("required_slots") or []
                ),
                "requirements": list(
                    awaiting_conversation.requirements_json or []
                ),
                "question_summary": reply or None,
            }
            session.last_agent_question = reply or None
            self.store.set_active_task_frame(
                session,
                awaiting_conversation,
            )
            return
        original_skill_id = str(pre_turn_state.get("active_skill_id") or "")
        original_records = [
            row for row in records if row.skill_id == original_skill_id
        ]
        if original_skill_id and not original_records:
            _restore_session_state(session, pre_turn_state)
            return
        candidates = [
            row
            for row in records
            if row.kind == "sop"
            and row.status
            not in {"completed", "cancelled", "failed"}
        ]
        if original_records:
            candidates.sort(
                key=lambda row: (
                    0 if row.skill_id == original_skill_id else 1,
                    row.sequence,
                )
            )
        else:
            candidates.sort(key=lambda row: row.sequence)
        if not candidates:
            conversation_candidates = [
                row
                for row in records
                if row.kind == "conversation"
                and row.status not in {"completed", "cancelled", "failed"}
            ]
            session.active_skill_id = None
            session.active_step_id = None
            session.slots_json = {}
            session.awaiting_input_json = None
            session.last_agent_question = None
            self.store.set_active_task_frame(
                session,
                (
                    sorted(
                        conversation_candidates,
                        key=lambda item: item.sequence,
                    )[0]
                    if conversation_candidates
                    else None
                ),
            )
            return
        row = candidates[0]
        result = row.result_json or {}
        reply = str(result.get("reply_fragment") or "").strip()
        self.owner.runtime.restore_task_frame(
            session,
            {
                "task_id": row.task_id,
                "skill_id": row.skill_id,
                "step_id": row.step_id,
                "slots": dict(row.slots_json or {}),
                "awaiting_input": (
                    {
                        "task_id": row.task_id,
                        "skill_id": row.skill_id,
                        "step_id": row.step_id,
                        "expected_fields": (
                            row.task_requirement_json.get("required_slots") or []
                        ),
                        "question_summary": reply or None,
                    }
                    if row.status == "awaiting_user"
                    else {}
                ),
            },
        )
        self.store.set_active_task_frame(session, row)
