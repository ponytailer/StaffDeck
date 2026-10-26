"""TaskFrame 的会话投影：project_session/planner_state/激活帧维护（批次 4.5 自 frames.py 拆出，mixin 化，零行为改动）。"""

from __future__ import annotations

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    new_id,
    utc_now,
)
from app.core.plan.frame_policy import MAX_TASK_FRAMES_PER_TURN
from app.session.session_schema import PlannedTaskFrame, TurnPlan

from app.core.engine.frame_defs import (
    DEPENDENCY_WAITING_ERROR_CODES,
    FRAME_LEASE_SECONDS,
    HARNESS_CONTEXT_KEY,
    TERMINAL_FRAME_STATUSES,
    TaskFrameClaimConflict,
)
from app.core.plan.frame_policy import MAX_TASK_FRAMES_PER_TURN
from app.session.session_schema import PlannedTaskFrame, TurnPlan



def _legacy_projection(row: HarnessTaskFrameRecord) -> dict[str, Any]:
    return {
        "task_id": row.task_id,
        "status": (
            "pending"
            if row.status in {"queued", "blocked", "action_budget"}
            else row.status
        ),
        "skill_id": row.skill_id,
        "target_skill_id": row.skill_id,
        "step_id": row.step_id,
        "target_step_id": row.step_id,
        "slots": dict(row.slots_json or {}),
        "slot_hints": dict(row.slots_json or {}),
        "intent_summary": row.user_intent,
        "user_intent": row.user_intent,
        "source_turn_id": row.source_turn_id,
        "resume_policy": "harness_v2",
        "updated_at": row.updated_at.isoformat(),
        "created_at": row.created_at.isoformat(),
    }


def _reference_scalar_values(value: Any) -> set[str]:
    """Return reference-like slot values without matching ordinary short entities."""

    return {
        item
        for item in _all_scalar_values(value)
        if len(item) >= 6
    }


def _all_scalar_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        collected: set[str] = set()
        for item in value.values():
            collected.update(_all_scalar_values(item))
        return collected
    if isinstance(value, list):
        collected = set()
        for item in value:
            collected.update(_all_scalar_values(item))
        return collected
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        normalized = str(value).strip()
        return {normalized} if normalized else set()
    return set()

class _FrameProjectionMixin:
    def project_session(self, session: ChatSession) -> None:
        rows = self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
            )
            .order_by(
                HarnessTaskFrameRecord.source_turn_id,
                HarnessTaskFrameRecord.sequence,
                HarnessTaskFrameRecord.created_at,
            )
        ).all()
        managed_ids = {row.task_id for row in rows}
        active_task_id = self.active_task_frame_id(session)
        legacy = [
            dict(item)
            for item in session.pending_tasks_json or []
            if isinstance(item, dict)
            and str(item.get("task_id") or "") not in managed_ids
        ]
        projected = [
            _legacy_projection(row)
            for row in rows
            if row.status not in TERMINAL_FRAME_STATUSES
            and row.kind == "sop"
            and row.task_id != active_task_id
        ]
        session.pending_tasks_json = [*legacy, *projected]
        session.updated_at = utc_now()
        self.db.add(session)

    def planner_state(self, session: ChatSession) -> list[dict[str, Any]]:
        rows = self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.status.notin_(TERMINAL_FRAME_STATUSES),
            )
            .order_by(
                HarnessTaskFrameRecord.updated_at.desc(),
                HarnessTaskFrameRecord.sequence,
            )
        ).all()
        active_task_id = self.active_task_frame_id(session)
        return [
            {
                "task_id": row.task_id,
                "kind": row.kind,
                "status": row.status,
                "skill_id": row.skill_id,
                "step_id": row.step_id,
                "user_intent": row.user_intent,
                "requirements": list(row.requirements_json or []),
                "slots": dict(row.slots_json or {}),
                "depends_on_task_ids": list(row.depends_on_json or []),
                "state_version": row.state_version,
                "active": row.task_id == active_task_id,
            }
            for row in rows
        ]

    def active_task_frame_id(self, session: ChatSession) -> str:
        state = (
            session.context_state_json
            if isinstance(session.context_state_json, dict)
            else {}
        )
        harness_state = state.get(HARNESS_CONTEXT_KEY)
        if isinstance(harness_state, dict):
            task_id = str(harness_state.get("active_task_frame_id") or "").strip()
            if task_id:
                return task_id
        awaiting = (
            session.awaiting_input_json
            if isinstance(session.awaiting_input_json, dict)
            else {}
        )
        return str(awaiting.get("task_id") or "").strip()

    def set_active_task_frame(
        self,
        session: ChatSession,
        row: HarnessTaskFrameRecord | None,
    ) -> None:
        state = dict(session.context_state_json or {})
        if row is None:
            state.pop(HARNESS_CONTEXT_KEY, None)
        else:
            state[HARNESS_CONTEXT_KEY] = {
                "active_task_frame_id": row.task_id,
                "kind": row.kind,
                "status": row.status,
                "state_version": row.state_version,
            }
        session.context_state_json = state
        session.updated_at = utc_now()
        self.db.add(session)

    def complete_active_frame(
        self,
        session: ChatSession,
        *,
        reason: str,
        task_id: str | None = None,
    ) -> HarnessTaskFrameRecord | None:
        resolved_task_id = str(
            task_id or self.active_task_frame_id(session) or ""
        ).strip()
        if not resolved_task_id:
            return None
        row = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.task_id == resolved_task_id,
            )
        ).first()
        if row is None or row.status in TERMINAL_FRAME_STATUSES:
            return row
        result = dict(row.result_json or {})
        result.update(
            {
                "task_frame_id": row.task_id,
                "status": "completed",
                "task_summary": reason,
            }
        )
        self.finish_frame(
            row,
            status="completed",
            step_id=row.step_id,
            slots=dict(row.slots_json or {}),
            result=result,
        )
        if resolved_task_id == self.active_task_frame_id(session):
            self.set_active_task_frame(session, None)
        return row

    def _apply_updates(self, session: ChatSession, plan: TurnPlan) -> None:
        for task_update in plan.task_updates:
            row = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == session.id,
                    HarnessTaskFrameRecord.task_id == task_update.task_id,
                )
            ).first()
            if row is None:
                continue
            if task_update.remove:
                row.status = "cancelled"
                row.lease_owner = None
                row.lease_expires_at = None
            elif task_update.status == "queued" and row.status not in {
                "running",
                *TERMINAL_FRAME_STATUSES,
            }:
                row.status = "queued"
            if task_update.user_intent is not None:
                row.user_intent = task_update.user_intent
            if task_update.slot_hints:
                row.slots_json = {
                    **dict(row.slots_json or {}),
                    **dict(task_update.slot_hints),
                }
            row.updated_at = utc_now()
            row.state_version += 1
            self.db.add(row)
