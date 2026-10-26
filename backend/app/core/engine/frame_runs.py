"""TaskFrame 的 AgentLoop/Run 生命周期：loop 收据、run 启停、来源轮取消（批次 4.5 自 frames.py 拆出，mixin 化，零行为改动）。"""

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


class _FrameRunsMixin:
    def ensure_agent_loop(
        self,
        row: HarnessTaskFrameRecord,
    ) -> HarnessAgentLoopRecord:
        """Return the durable logical loop that owns this TaskFrame."""

        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id) if row.agent_loop_id else None
        loop_key = f"sop:{row.id}" if row.kind == "sop" else f"general:{row.session_id}"
        if loop is None:
            loop = self.db.exec(
                select(HarnessAgentLoopRecord).where(
                    HarnessAgentLoopRecord.session_id == row.session_id,
                    HarnessAgentLoopRecord.loop_key == loop_key,
                )
            ).first()
        if loop is None:
            loop = HarnessAgentLoopRecord(
                tenant_id=row.tenant_id,
                session_id=row.session_id,
                loop_key=loop_key,
                kind="sop" if row.kind == "sop" else "general",
                status="active",
                owner_task_frame_record_id=row.id,
                skill_id=row.skill_id,
                workspace_scope_id=row.id,
            )
            self.db.add(loop)
            self.db.flush()
        else:
            loop.owner_task_frame_record_id = row.id
            loop.skill_id = row.skill_id
            loop.status = "active"
            loop.finished_at = None
            loop.updated_at = utc_now()
            loop.state_version = max(1, int(loop.state_version or 0) + 1)
            self.db.add(loop)
        if row.agent_loop_id != loop.id:
            row.agent_loop_id = loop.id
            row.updated_at = utc_now()
            self.db.add(row)
            self.db.flush()
        return loop

    def save_agent_loop_checkpoint(
        self,
        loop: HarnessAgentLoopRecord,
        checkpoint: dict[str, Any],
        *,
        status: str | None = None,
        last_run_id: str | None = None,
    ) -> None:
        loop.checkpoint_json = dict(checkpoint)
        if status is not None:
            loop.status = status
        if last_run_id is not None:
            loop.last_run_id = last_run_id
        loop.updated_at = utc_now()
        loop.state_version = max(1, int(loop.state_version or 0) + 1)
        if loop.status in {"completed", "failed", "cancelled"}:
            loop.finished_at = utc_now()
        else:
            loop.finished_at = None
        self.db.add(loop)

    def finish_agent_loop_for_frame(
        self,
        row: HarnessTaskFrameRecord,
        *,
        result_status: str,
        checkpoint: dict[str, Any],
        last_run_id: str | None,
    ) -> None:
        if not row.agent_loop_id:
            return
        loop = self.db.get(HarnessAgentLoopRecord, row.agent_loop_id)
        if loop is None:
            return
        if result_status == "awaiting_user":
            loop_status = "suspended"
        elif row.kind != "sop":
            loop_status = "active"
        elif result_status == "completed":
            loop_status = "completed"
        elif result_status == "cancelled":
            loop_status = "cancelled"
        elif result_status == "failed":
            loop_status = "failed"
        else:
            loop_status = "active"
        self.save_agent_loop_checkpoint(
            loop,
            checkpoint,
            status=loop_status,
            last_run_id=last_run_id,
        )

    def start_run(
        self,
        row: HarnessTaskFrameRecord,
        *,
        requirement: dict[str, Any],
        capability_snapshot: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> HarnessRunRecord:
        expected_owner = lease_owner or row.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(row.attempt_no or 0)
        )
        run = HarnessRunRecord(
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            task_frame_record_id=row.id,
            agent_loop_id=row.agent_loop_id,
            task_id=row.task_id,
            source_turn_id=row.source_turn_id,
            status="running",
            attempt_no=expected_attempt,
            lease_owner=expected_owner,
            lease_expires_at=row.lease_expires_at,
            task_requirement_json=requirement,
            capability_snapshot_json=capability_snapshot,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def update_run_context(
        self,
        run: HarnessRunRecord,
        *,
        requirement: dict[str, Any],
        capability_snapshot: dict[str, Any],
    ) -> None:
        """Refresh the current-node projection without creating another run."""

        run.task_requirement_json = dict(requirement)
        run.capability_snapshot_json = dict(capability_snapshot)
        run.updated_at = utc_now()
        self.db.add(run)

    def finish_run(
        self,
        run: HarnessRunRecord,
        *,
        status: str,
        action_count: int,
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> None:
        expected_owner = lease_owner or run.lease_owner
        expected_attempt = (
            int(attempt_no)
            if attempt_no is not None
            else int(run.attempt_no or 0)
        )
        if not expected_owner:
            raise TaskFrameClaimConflict(
                f"Harness run {run.id} no longer owns a running lease."
            )
        now = utc_now()
        statement = (
            update(HarnessRunRecord)
            .where(
                HarnessRunRecord.id == run.id,
                HarnessRunRecord.status == "running",
                HarnessRunRecord.lease_owner == expected_owner,
                HarnessRunRecord.attempt_no == expected_attempt,
                HarnessRunRecord.lease_expires_at > now,
            )
            .values(
                status=status,
                action_count=max(0, int(action_count)),
                result_json=dict(result),
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        with self.db.no_autoflush:
            update_result = self.db.exec(statement)
        if getattr(update_result, "rowcount", 0) != 1:
            self.db.rollback()
            raise TaskFrameClaimConflict(
                f"Harness run {run.id} completion was fenced."
            )
        self.db.refresh(run)

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

    def latest_awaiting_conversation(
        self,
        session: ChatSession,
    ) -> HarnessTaskFrameRecord | None:
        return self.db.exec(
            select(HarnessTaskFrameRecord)
            .where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.kind == "conversation",
                HarnessTaskFrameRecord.status == "awaiting_user",
            )
            .order_by(
                HarnessTaskFrameRecord.updated_at.desc(),
                HarnessTaskFrameRecord.created_at.desc(),
                HarnessTaskFrameRecord.sequence.desc(),
            )
        ).first()

    def finish_source_turn_running_runs(
        self,
        session_id: str,
        source_turn_id: str,
        *,
        status: str,
        result: dict[str, Any],
        lease_owner: str | None = None,
        attempt_no: int | None = None,
    ) -> list[HarnessRunRecord]:
        filters = [
            HarnessRunRecord.session_id == session_id,
            HarnessRunRecord.source_turn_id == source_turn_id,
            HarnessRunRecord.status == "running",
        ]
        if lease_owner is not None:
            filters.append(HarnessRunRecord.lease_owner == lease_owner)
        if attempt_no is not None:
            filters.append(HarnessRunRecord.attempt_no == attempt_no)
        runs = self.db.exec(
            select(HarnessRunRecord).where(*filters)
        ).all()
        for run in runs:
            self.finish_run(
                run,
                status=status,
                action_count=run.action_count,
                result=dict(result),
            )
        return list(runs)

    def cancel_source_turn(
        self,
        session: ChatSession,
        source_turn_id: str,
    ) -> list[HarnessTaskFrameRecord]:
        """Cancel every nonterminal frame and running attempt created by one turn."""

        self.finish_source_turn_running_runs(
            session.id,
            source_turn_id,
            status="cancelled",
            result={
                "status": "cancelled",
                "task_summary": "用户取消了当前 Harness 执行。",
            },
        )
        rows = self.db.exec(
            select(HarnessTaskFrameRecord).where(
                HarnessTaskFrameRecord.tenant_id == session.tenant_id,
                HarnessTaskFrameRecord.session_id == session.id,
                HarnessTaskFrameRecord.source_turn_id == source_turn_id,
                HarnessTaskFrameRecord.status.notin_(TERMINAL_FRAME_STATUSES),
            )
        ).all()
        cancelled_task_ids: set[str] = set()
        for row in rows:
            cancelled_task_ids.add(row.task_id)
            self.finish_frame(
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
            self.finish_agent_loop_for_frame(
                row,
                result_status="cancelled",
                checkpoint=self.agent_loop_checkpoint(row),
                last_run_id=None,
            )
        if self.active_task_frame_id(session) in cancelled_task_ids:
            self.set_active_task_frame(session, None)
        self.project_session(session)
        return list(rows)

    def _abandon_stale_runs(self, row: HarnessTaskFrameRecord) -> None:
        runs = self.db.exec(
            select(HarnessRunRecord).where(
                HarnessRunRecord.task_frame_record_id == row.id,
                HarnessRunRecord.status == "running",
            )
        ).all()
        for run in runs:
            run.status = "abandoned"
            run.result_json = {
                "status": "abandoned",
                "error": {"code": "LEASE_EXPIRED"},
            }
            run.finished_at = utc_now()
            run.updated_at = utc_now()
            run.lease_owner = None
            run.lease_expires_at = None
            self.db.add(run)
