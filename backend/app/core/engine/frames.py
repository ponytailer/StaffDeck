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
from app.core.engine.frame_projection import (  # noqa: F401  (re-export)
    _all_scalar_values,
    _legacy_projection,
    _reference_scalar_values,
)
from app.core.engine.frame_projection import _FrameProjectionMixin
from app.core.engine.frame_runs import _FrameRunsMixin


TERMINAL_FRAME_STATUSES = {"completed", "cancelled", "failed"}
DEPENDENCY_WAITING_ERROR_CODES = {
    "DEPENDENCY_BLOCKED",  # compatibility with the first Harness v2 scheduler
    "DEPENDENCY_WAITING",
}
HARNESS_CONTEXT_KEY = "harness_v2"
FRAME_LEASE_SECONDS = 900


class TaskFrameStore(_FrameRunsMixin, _FrameProjectionMixin):
    def __init__(self, db: Session):
        self.db = db

    def persist_plan(
        self,
        session: ChatSession,
        source_turn_id: str,
        plan: TurnPlan,
    ) -> list[HarnessTaskFrameRecord]:
        self._apply_updates(session, plan)
        bounded_frames = list(plan.task_frames[:MAX_TASK_FRAMES_PER_TURN])
        bounded_task_ids = {
            str(frame.task_id or "").strip()
            for frame in bounded_frames
            if str(frame.task_id or "").strip()
        }
        records: list[HarnessTaskFrameRecord] = []
        for sequence, frame in enumerate(bounded_frames):
            task_id = str(frame.task_id or "").strip()
            if not task_id:
                continue
            row = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == session.id,
                    HarnessTaskFrameRecord.task_id == task_id,
                )
            ).first()
            created = row is None
            if created:
                row = HarnessTaskFrameRecord(
                    tenant_id=session.tenant_id,
                    session_id=session.id,
                    source_turn_id=source_turn_id,
                    task_id=task_id,
                )
            elif row.status in TERMINAL_FRAME_STATUSES:
                continue
            elif row.status == "running":
                if (
                    row.lease_expires_at is not None
                    and row.lease_expires_at <= utc_now()
                ):
                    self._abandon_stale_runs(row)
                    row.status = "queued"
                    row.lease_owner = None
                    row.lease_expires_at = None
                else:
                    raise RuntimeError(
                        f"TaskFrame {row.task_id} is already running."
                    )
            row.source_turn_id = source_turn_id
            if created:
                row.kind = frame.kind
            row.decision = frame.decision
            row.status = "queued"
            row.sequence = sequence
            if created:
                row.skill_id = frame.target_skill_id
            continues_active = bool(
                frame.kind == "sop"
                and frame.decision == "continue_active"
                and frame.target_skill_id == session.active_skill_id
            )
            if created:
                row.step_id = (
                    session.active_step_id
                    if continues_active and session.active_step_id
                    else frame.target_step_id
                )
            row.user_intent = frame.user_intent
            row.requirements_json = list(frame.requirements)
            base_slots = (
                dict(session.slots_json or {})
                if continues_active
                else dict(row.slots_json or {})
            )
            row.slots_json = {**base_slots, **dict(frame.slot_hints)}
            planned_dependencies = [
                dependency_id
                for dependency_id in frame.depends_on_task_ids
                if dependency_id in bounded_task_ids
                and dependency_id != task_id
            ]
            if created or planned_dependencies:
                row.depends_on_json = planned_dependencies
            row.error_json = {}
            row.updated_at = utc_now()
            row.state_version = max(1, int(row.state_version or 0) + 1)
            self.db.add(row)
            records.append(row)
        self.db.flush()
        self.project_session(session)
        return sorted(records, key=lambda row: row.sequence)

    def mark_running(self, row: HarnessTaskFrameRecord) -> None:
        lease_owner = new_id("hlease")
        expected_version = int(row.state_version or 0)
        attempt_no = max(0, int(row.attempt_no or 0)) + 1
        now = utc_now()
        lease_expires_at = now + timedelta(
            seconds=FRAME_LEASE_SECONDS
        )
        statement = (
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "queued",
                HarnessTaskFrameRecord.state_version == expected_version,
            )
            .values(
                status="running",
                attempt_no=attempt_no,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                updated_at=now,
                state_version=expected_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        result = self.db.exec(statement)
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} was claimed by another turn."
            )
        self.db.refresh(row)

    def save_requirement(
        self,
        row: HarnessTaskFrameRecord,
        payload: dict[str, Any],
        *,
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        expected_owner = lease_owner or row.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(row.attempt_no or 0)
        )
        if not expected_owner:
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} no longer owns a running lease."
            )
        now = utc_now()
        statement = (
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "running",
                HarnessTaskFrameRecord.lease_owner == expected_owner,
                HarnessTaskFrameRecord.attempt_no == expected_attempt,
                HarnessTaskFrameRecord.lease_expires_at > now,
            )
            .values(
                task_requirement_json=payload,
                step_id=row.step_id,
                slots_json=dict(row.slots_json or {}),
                lease_expires_at=now + timedelta(seconds=FRAME_LEASE_SECONDS),
                updated_at=now,
                state_version=HarnessTaskFrameRecord.state_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        with self.db.no_autoflush:
            result = self.db.exec(statement)
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} lease renewal was fenced."
            )
        self.db.refresh(row)

    def renew_running_lease(
        self,
        row: HarnessTaskFrameRecord,
        *,
        lease_owner: str,
        attempt_no: int,
    ) -> None:
        """Renew one claimed frame and its active run under the same fence."""

        if not lease_owner:
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} no longer owns a running lease."
            )
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=FRAME_LEASE_SECONDS)
        result = self.db.exec(
            update(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.id == row.id,
                HarnessTaskFrameRecord.status == "running",
                HarnessTaskFrameRecord.lease_owner == lease_owner,
                HarnessTaskFrameRecord.attempt_no == attempt_no,
                HarnessTaskFrameRecord.lease_expires_at > now,
            )
            .values(
                lease_expires_at=lease_expires_at,
                updated_at=now,
                state_version=HarnessTaskFrameRecord.state_version + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"TaskFrame {row.task_id} lease renewal was fenced."
            )
        self.db.exec(
            update(HarnessRunRecord)
            .where(
                HarnessRunRecord.task_frame_record_id == row.id,
                HarnessRunRecord.status == "running",
                HarnessRunRecord.lease_owner == lease_owner,
                HarnessRunRecord.attempt_no == attempt_no,
            )
            .values(
                lease_expires_at=lease_expires_at,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self.db.refresh(row)

    def finish_frame(
        self,
        row: HarnessTaskFrameRecord,
        *,
        status: str,
        step_id: str | None,
        slots: dict[str, Any],
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        error = result.get("error")
        error_json = dict(error) if isinstance(error, dict) else {}
        should_fence = lease_owner is not None or row.status == "running"
        if should_fence:
            expected_owner = lease_owner or row.lease_owner
            expected_attempt = (
                int(attempt_no)
                if attempt_no is not None
                else int(row.attempt_no or 0)
            )
            if not expected_owner:
                raise TaskFrameClaimConflict(
                    f"TaskFrame {row.task_id} has no lease owner."
                )
            now = utc_now()
            statement = (
                update(HarnessTaskFrameRecord)
                .where(
                    HarnessTaskFrameRecord.id == row.id,
                    HarnessTaskFrameRecord.status == "running",
                    HarnessTaskFrameRecord.lease_owner == expected_owner,
                    HarnessTaskFrameRecord.attempt_no == expected_attempt,
                    HarnessTaskFrameRecord.lease_expires_at > now,
                )
                .values(
                    status=status,
                    step_id=step_id,
                    slots_json=dict(slots),
                    result_json=dict(result),
                    error_json=error_json,
                    updated_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    state_version=HarnessTaskFrameRecord.state_version + 1,
                )
                .execution_options(synchronize_session=False)
            )
            with self.db.no_autoflush:
                update_result = self.db.exec(statement)
            if getattr(update_result, "rowcount", 0) != 1:
                self.db.rollback()
                raise TaskFrameClaimConflict(
                    f"TaskFrame {row.task_id} completion was fenced."
                )
            self.db.refresh(row)
            return
        row.status = status
        row.step_id = step_id
        row.slots_json = dict(slots)
        row.result_json = result
        row.error_json = error_json
        row.updated_at = utc_now()
        row.lease_owner = None
        row.lease_expires_at = None
        row.state_version += 1
        self.db.add(row)

    def defer_for_action_budget(
        self,
        rows: list[HarnessTaskFrameRecord],
    ) -> None:
        """Keep unstarted frames durable and resumable after the turn budget ends."""

        for row in rows:
            if row.status in TERMINAL_FRAME_STATUSES:
                continue
            row.status = "queued"
            row.result_json = {
                "task_frame_id": row.task_id,
                "status": "action_budget",
                "task_summary": (
                    "本轮共享 action budget 已耗尽，TaskFrame 尚未执行并保持排队。"
                ),
            }
            row.error_json = {"code": "TURN_ACTION_BUDGET_DEFERRED"}
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = utc_now()
            row.state_version += 1
            self.db.add(row)

    def agent_loop_checkpoint(
        self,
        row: HarnessTaskFrameRecord,
    ) -> dict[str, Any]:
        if not row.agent_loop_id:
            return {}
        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id)
        return dict(loop.checkpoint_json or {}) if loop is not None else {}

    def dependencies_satisfied(
        self,
        row: HarnessTaskFrameRecord,
        records: list[HarnessTaskFrameRecord],
    ) -> bool:
        dependency_ids = {
            str(task_id)
            for task_id in row.depends_on_json or []
            if str(task_id).strip()
        }
        if not dependency_ids:
            return True
        status_by_id = {item.task_id: item.status for item in records}
        missing_ids = dependency_ids - set(status_by_id)
        if missing_ids:
            persisted = self.db.exec(
                select(HarnessTaskFrameRecord).where(
                    HarnessTaskFrameRecord.session_id == row.session_id,
                    HarnessTaskFrameRecord.task_id.in_(missing_ids),
                )
            ).all()
            status_by_id.update(
                {item.task_id: item.status for item in persisted}
            )
        return all(
            status_by_id.get(task_id) == "completed"
            for task_id in dependency_ids
        )

    def defer_for_dependencies(
        self,
        row: HarnessTaskFrameRecord,
    ) -> None:
        """Keep a dependent frame queued until every prerequisite completes."""

        row.status = "queued"
        row.result_json = {
            "task_frame_id": row.task_id,
            "status": "blocked",
            "reply_fragment": "前置任务完成后将自动继续该任务。",
            "task_summary": "TaskFrame 正在等待前置任务完成。",
            "action_count": 0,
            "error": {"code": "DEPENDENCY_WAITING"},
        }
        row.error_json = {"code": "DEPENDENCY_WAITING"}
        row.lease_owner = None
        row.lease_expires_at = None
        row.updated_at = utc_now()
        row.state_version += 1
        self.db.add(row)

    def ready_dependency_frames(
        self,
        session: ChatSession,
        *,
        exclude_task_ids: set[str] | None = None,
    ) -> list[HarnessTaskFrameRecord]:
        """Return durable follow-up frames whose prerequisites are complete.

        Old ``blocked/DEPENDENCY_BLOCKED`` rows are repaired here so sessions
        created before dependency waiting became resumable are not stranded.
        """

        excluded = set(exclude_task_ids or set())
        all_rows = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
            )
        ).all()
        status_by_id = {item.task_id: item.status for item in all_rows}
        candidates: list[HarnessTaskFrameRecord] = []
        for item in all_rows:
            dependency_ids = [
                str(task_id)
                for task_id in item.depends_on_json or []
                if str(task_id).strip()
            ]
            if (
                item.task_id in excluded
                or not dependency_ids
                or item.status not in {"queued", "blocked"}
            ):
                continue
            if item.status == "blocked" and str(
                (item.error_json or {}).get("code") or ""
            ) not in DEPENDENCY_WAITING_ERROR_CODES:
                continue
            if not all(
                status_by_id.get(task_id) == "completed"
                for task_id in dependency_ids
            ):
                continue
            if item.status == "blocked":
                item.status = "queued"
                item.result_json = {}
                item.error_json = {}
                item.updated_at = utc_now()
                item.state_version += 1
                self.db.add(item)
            candidates.append(item)
        self.db.flush()
        return sorted(
            candidates,
            key=lambda item: (
                item.created_at,
                item.sequence,
                item.task_id,
            ),
        )

    def dependency_results(
        self,
        row: HarnessTaskFrameRecord,
    ) -> list[dict[str, Any]]:
        """Project completed prerequisite results into the child TaskRequirement."""

        dependency_ids = [
            str(task_id)
            for task_id in row.depends_on_json or []
            if str(task_id).strip()
        ]
        if not dependency_ids:
            return []
        dependencies = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.session_id == row.session_id,
                HarnessTaskFrameRecord.task_id.in_(set(dependency_ids)),
                HarnessTaskFrameRecord.status == "completed",
            )
        ).all()
        by_id = {item.task_id: item for item in dependencies}
        projected: list[dict[str, Any]] = []
        for task_id in dependency_ids:
            dependency = by_id.get(task_id)
            if dependency is None:
                continue
            result = (
                dependency.result_json
                if isinstance(dependency.result_json, dict)
                else {}
            )
            projected.append(
                {
                    "task_frame_id": task_id,
                    "status": dependency.status,
                    "task_summary": result.get("task_summary"),
                    "slot_updates": result.get("slot_updates") or {},
                    "capability_results": result.get("capability_results") or [],
                    "artifacts": result.get("artifacts") or [],
                }
            )
        return projected

    def referenced_session_results(
        self,
        row: HarnessTaskFrameRecord,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Resolve durable prior capability results referenced by this frame's slots.

        A later TaskFrame may refer to an identifier produced by an earlier, unrelated
        frame (for example an order, document, ticket, or job id). Those frames do not
        have a planner dependency edge, but the model still needs the authoritative
        result behind the identifier instead of passing the opaque id to an unrelated
        lookup tool. Matching is exact, session-scoped, and only considers completed
        invocations, so no conversational history or sibling task is leaked.
        """

        reference_values = _reference_scalar_values(row.slots_json or {})
        if not reference_values:
            return []
        invocations = self.db.exec(
            select(HarnessInvocationRecord)
            .where(
                HarnessInvocationRecord.tenant_id == row.tenant_id,
                HarnessInvocationRecord.session_id == row.session_id,
                HarnessInvocationRecord.task_id != row.task_id,
                HarnessInvocationRecord.status == "completed",
            )
            .order_by(HarnessInvocationRecord.finished_at.desc())
            .limit(80)
        ).all()
        projected: list[dict[str, Any]] = []
        for invocation in invocations:
            result = (
                dict(invocation.response_cache_json or {})
                if isinstance(invocation.response_cache_json, dict)
                else {}
            )
            matches = sorted(reference_values.intersection(_all_scalar_values(result)))
            if not matches:
                continue
            projected.append(
                {
                    "task_frame_id": invocation.task_id,
                    "status": "completed",
                    "task_summary": (
                        f"此前能力 {invocation.tool_name} 的结果与当前任务引用匹配。"
                    ),
                    "slot_updates": {},
                    "capability_results": [
                        {
                            "tool_name": invocation.tool_name,
                            "arguments": dict(invocation.arguments_json or {}),
                            "result": result,
                        }
                    ],
                    "artifacts": [],
                    "reference_matches": matches,
                    "reference_source": "session_invocation",
                }
            )
            if len(projected) >= max(1, int(limit)):
                break
        return projected

def planned_frame_from_record(row: HarnessTaskFrameRecord) -> PlannedTaskFrame:
    decision = row.decision or (
        "answer_only"
        if row.kind == "conversation"
        else ("continue_active" if row.step_id else "start_new_task")
    )
    return PlannedTaskFrame(
        task_id=row.task_id,
        kind=row.kind,  # type: ignore[arg-type]
        status=(
            row.status
            if row.status
            in {
                "queued",
                "running",
                "awaiting_user",
                "blocked",
                "completed",
                "handoff",
                "failed",
                "cancelled",
            }
            else "queued"
        ),
        decision=decision,
        target_skill_id=row.skill_id,
        target_step_id=row.step_id,
        user_intent=row.user_intent,
        requirements=list(row.requirements_json or []),
        slot_hints=dict(row.slots_json or {}),
        depends_on_task_ids=list(row.depends_on_json or []),
    )

