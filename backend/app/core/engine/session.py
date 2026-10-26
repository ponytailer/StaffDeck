"""会话级并发控制：进程内锁、数据库租约、轮次回执、记录与工作区清理。

批次 4.1 由原 ``engine/session/`` 包四文件合并而来（lock / lease / turn_store / cleanup），
纯搬不改；导入路径统一为 ``app.core.engine.session``。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import delete, update
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import paths
from app.db.bulk_delete import bulk_delete_matching
from app.db.models import (
    ChatSession,
    HarnessAgentLoopRecord,
    HarnessInvocationRecord,
    HarnessRunRecord,
    HarnessSessionLeaseRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    UIConfig,
    new_id,
    utc_now,
)
from app.session.session_schema import ChatTurnRequest, ChatTurnResponse

# ---------------------------------------------------------------------------
# lock（原 session/lock.py）
# ---------------------------------------------------------------------------


class HarnessSessionBusy(RuntimeError):
    pass


_registry_lock = Lock()
_session_locks: dict[str, Lock] = {}


def acquire_harness_session(session_id: str) -> Lock:
    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id is required")
    with _registry_lock:
        lock = _session_locks.setdefault(normalized, Lock())
    if not lock.acquire(blocking=False):
        raise HarnessSessionBusy(
            "该会话已有一个 Harness 执行正在进行，请等待其结束后重试。"
        )
    return lock


def release_harness_session(session_id: str, lock: Lock | None) -> None:
    if lock is None:
        return
    lock.release()


# ---------------------------------------------------------------------------
# lease（原 session/lease.py）
# ---------------------------------------------------------------------------


SESSION_LEASE_SECONDS = 900


class HarnessSessionLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessSessionLeaseToken:
    record_id: str
    tenant_id: str
    session_id: str
    lease_owner: str


class HarnessSessionLeaseStore:
    """Database-backed session mutex with owner fencing and expiration."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def acquire(self, session: ChatSession) -> HarnessSessionLeaseToken:
        now = utc_now()
        row = HarnessSessionLeaseRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            lease_owner=new_id("hsleaseowner"),
            lease_expires_at=now + timedelta(seconds=SESSION_LEASE_SECONDS),
        )
        self.db.add(row)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return self._take_expired(session)
        return _lease_token(row)

    def renew(self, lease: HarnessSessionLeaseToken | None) -> None:
        if lease is None:
            raise HarnessSessionLeaseLost("Harness session lease is missing.")
        now = utc_now()
        result = self.db.exec(
            update(HarnessSessionLeaseRecord)
            .where(
                HarnessSessionLeaseRecord.id == lease.record_id,
                HarnessSessionLeaseRecord.tenant_id == lease.tenant_id,
                HarnessSessionLeaseRecord.session_id == lease.session_id,
                HarnessSessionLeaseRecord.lease_owner == lease.lease_owner,
                HarnessSessionLeaseRecord.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now
                + timedelta(seconds=SESSION_LEASE_SECONDS),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise HarnessSessionLeaseLost(
                "Harness session execution lease was fenced by another worker."
            )
        self.db.flush()

    def release(self, lease: HarnessSessionLeaseToken | None) -> None:
        if lease is None:
            return
        self.db.rollback()
        self.db.exec(
            delete(HarnessSessionLeaseRecord).where(
                HarnessSessionLeaseRecord.id == lease.record_id,
                HarnessSessionLeaseRecord.lease_owner == lease.lease_owner,
            )
        )
        self.db.commit()

    def _take_expired(
        self,
        session: ChatSession,
    ) -> HarnessSessionLeaseToken:
        now = utc_now()
        owner = new_id("hsleaseowner")
        result = self.db.exec(
            update(HarnessSessionLeaseRecord)
            .where(
                HarnessSessionLeaseRecord.tenant_id == session.tenant_id,
                HarnessSessionLeaseRecord.session_id == session.id,
                HarnessSessionLeaseRecord.lease_expires_at <= now,
            )
            .values(
                lease_owner=owner,
                lease_expires_at=now
                + timedelta(seconds=SESSION_LEASE_SECONDS),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            raise HarnessSessionBusy(
                "该会话已有一个 Harness 执行正在进行，请等待其结束后重试。"
            )
        self.db.commit()
        row = self.db.exec(
            select(HarnessSessionLeaseRecord).where(
                HarnessSessionLeaseRecord.tenant_id == session.tenant_id,
                HarnessSessionLeaseRecord.session_id == session.id,
                HarnessSessionLeaseRecord.lease_owner == owner,
            )
        ).one()
        return _lease_token(row)


def _lease_token(row: HarnessSessionLeaseRecord) -> HarnessSessionLeaseToken:
    return HarnessSessionLeaseToken(
        record_id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        lease_owner=row.lease_owner,
    )


# ---------------------------------------------------------------------------
# turn_store（原 session/turn_store.py）
# ---------------------------------------------------------------------------


TURN_LEASE_SECONDS = 900


class HarnessTurnConflict(RuntimeError):
    pass


class HarnessTurnClaim:
    def __init__(
        self,
        *,
        record: HarnessTurnRecord | None,
        replay: ChatTurnResponse | None = None,
    ) -> None:
        self.record = record
        self.replay = replay


class HarnessTurnStore:
    """Durable exactly-once receipts keyed by the caller's client_turn_id."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def claim(
        self,
        session: ChatSession,
        request: ChatTurnRequest,
    ) -> HarnessTurnClaim:
        client_turn_id = str(request.client_turn_id or "").strip()
        if not client_turn_id:
            return HarnessTurnClaim(record=None)
        digest = _request_digest(request)
        existing = self._find(session, client_turn_id)
        if existing is not None:
            return self._existing_claim(existing, digest)

        now = utc_now()
        record = HarnessTurnRecord(
            tenant_id=session.tenant_id,
            session_id=session.id,
            client_turn_id=client_turn_id,
            request_digest=digest,
            lease_owner=new_id("hturnlease"),
            lease_expires_at=now + timedelta(seconds=TURN_LEASE_SECONDS),
        )
        self.db.add(record)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            existing = self._find(session, client_turn_id)
            if existing is None:
                raise
            return self._existing_claim(existing, digest)
        self.db.refresh(record)
        return HarnessTurnClaim(record=record)

    def bind_user_message(
        self,
        record: HarnessTurnRecord | None,
        user_message_id: str,
    ) -> None:
        if record is None:
            return
        self._fenced_update(
            record,
            values={
                "user_message_id": user_message_id,
                "updated_at": utc_now(),
            },
        )

    def renew(self, record: HarnessTurnRecord | None) -> None:
        """Keep the durable turn receipt alive while its executor is healthy."""

        if record is None or record.status not in {"started", "finalizing"}:
            return
        now = utc_now()
        self._fenced_update(
            record,
            expected_statuses=("started", "finalizing"),
            values={
                "lease_expires_at": now + timedelta(seconds=TURN_LEASE_SECONDS),
                "updated_at": now,
            },
        )

    def complete(
        self,
        record: HarnessTurnRecord | None,
        response: ChatTurnResponse,
    ) -> None:
        if record is None:
            return
        now = utc_now()
        self._fenced_update(
            record,
            # ``started`` remains accepted for compatibility with non-projecting
            # callers; the Harness v2 response path always reserves
            # ``finalizing`` first.
            expected_statuses=("started", "finalizing"),
            values={
                "status": "completed",
                "response_json": response.model_dump(mode="json"),
                "finished_at": now,
                "updated_at": now,
            },
        )

    def begin_completion(self, record: HarnessTurnRecord | None) -> None:
        """Acquire the durable right to publish the normal terminal reply."""

        if record is None:
            return
        self._fenced_update(
            record,
            expected_statuses=("started",),
            values={"status": "finalizing", "updated_at": utc_now()},
        )

    def cancel(
        self,
        record: HarnessTurnRecord | None,
        *,
        message: str = "用户取消了当前 Harness 执行。",
    ) -> bool:
        """Atomically make cancellation the terminal owner for a receipt."""

        if record is None:
            return False
        now = utc_now()
        return self._try_fenced_update(
            record,
            expected_statuses=("started",),
            values={
                "status": "cancelled",
                "error_json": {"code": "CANCELLED", "message": message},
                "finished_at": now,
                "updated_at": now,
            },
        )

    def finish_with_error(
        self,
        record: HarnessTurnRecord | None,
        *,
        status: str,
        code: str,
        message: str,
    ) -> None:
        if record is None or record.status not in {"started", "finalizing"}:
            return
        now = utc_now()
        self._fenced_update(
            record,
            expected_statuses=("started", "finalizing"),
            values={
                "status": status,
                "error_json": {
                    "code": code,
                    "message": str(message)[:2_000],
                },
                "finished_at": now,
                "updated_at": now,
            },
        )

    def _find(
        self,
        session: ChatSession,
        client_turn_id: str,
    ) -> HarnessTurnRecord | None:
        return self.db.exec(
            select(HarnessTurnRecord).where(
                HarnessTurnRecord.tenant_id == session.tenant_id,
                HarnessTurnRecord.session_id == session.id,
                HarnessTurnRecord.client_turn_id == client_turn_id,
            )
        ).first()

    def _existing_claim(
        self,
        existing: HarnessTurnRecord,
        request_digest: str,
    ) -> HarnessTurnClaim:
        if existing.request_digest != request_digest:
            raise HarnessTurnConflict(
                "同一个 client_turn_id 不能用于不同的 Harness 请求。"
            )
        if existing.status == "completed" and existing.response_json:
            return HarnessTurnClaim(
                record=existing,
                replay=ChatTurnResponse.model_validate(existing.response_json),
            )
        if existing.status in {"started", "finalizing"}:
            state = (
                "仍在执行"
                if existing.lease_expires_at > utc_now()
                else "执行结果未知，需先核对执行记录"
            )
            raise HarnessTurnConflict(
                f"该 client_turn_id 对应的 Harness turn {state}，不会重复执行。"
            )
        raise HarnessTurnConflict(
            "该 client_turn_id 已结束且不能自动重试；请使用新的 client_turn_id。"
        )

    def _fenced_update(
        self,
        record: HarnessTurnRecord,
        *,
        expected_statuses: tuple[str, ...] = ("started",),
        values: dict[str, Any],
    ) -> None:
        if self._try_fenced_update(
            record,
            expected_statuses=expected_statuses,
            values=values,
        ):
            return
        raise HarnessTurnConflict("Harness turn receipt 已由其他执行者更新。")

    def _try_fenced_update(
        self,
        record: HarnessTurnRecord,
        *,
        expected_statuses: tuple[str, ...],
        values: dict[str, Any],
    ) -> bool:
        result = self.db.exec(
            update(HarnessTurnRecord)
            .where(
                HarnessTurnRecord.id == record.id,
                HarnessTurnRecord.status.in_(expected_statuses),
                HarnessTurnRecord.lease_owner == record.lease_owner,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            self.db.rollback()
            self.db.refresh(record)
            return False
        self.db.commit()
        self.db.refresh(record)
        return True


def _request_digest(request: ChatTurnRequest) -> str:
    payload = request.model_dump(
        mode="json",
        exclude={"session_id", "client_turn_id"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# cleanup（原 session/cleanup.py）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessSessionRecordCleanup:
    session_lease_count: int
    turn_count: int
    invocation_count: int
    run_count: int
    task_frame_count: int
    # 后加的字段给默认值：老的构造点（含测试里的假数据）不需要同步改。
    agent_loop_count: int = 0


def _delete_session_rows(
    db: Session, model: type, *, tenant_id: str, session_id: str
) -> int:
    """按 tenant_id + session_id 批量删一张表，返回命中行数。

    刻意用 Core 的 DELETE 而不是「SELECT 出行再 db.delete(row)」：后者会把整行
    （``agent_events.payload_json`` / ``messages.content_json`` 这类字段单会话可到 MB 级）
    读进 Python 只是为了丢掉，实测单会话删除从 4.6s 降到 0.08s。
    ``bulk_delete_matching`` 会顺带把会话内已加载的同批对象摘出 identity map，
    避免调用方随后读属性时去刷新已删除的行。
    """
    return bulk_delete_matching(
        db, model, tenant_id=tenant_id, session_id=session_id
    )


def stage_harness_session_record_deletion(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> HarnessSessionRecordCleanup:
    """Stage Harness v2 records for deletion in dependency order.

    The caller owns the surrounding transaction so chat-session deletion remains
    atomic with the existing message, event, and feedback cleanup.

    删除顺序即依赖顺序（invocation → run → task_frame → agent_loop → turn → lease）。
    库里这些表之间没有外键约束，顺序只影响可读性；``harness_agent_loops`` 曾经被漏删，
    导致会话删掉后留下 active 的孤儿 loop（本次一并修掉）。
    """

    invocation_count = _delete_session_rows(
        db, HarnessInvocationRecord, tenant_id=tenant_id, session_id=session_id
    )
    run_count = _delete_session_rows(
        db, HarnessRunRecord, tenant_id=tenant_id, session_id=session_id
    )
    task_frame_count = _delete_session_rows(
        db, HarnessTaskFrameRecord, tenant_id=tenant_id, session_id=session_id
    )
    agent_loop_count = _delete_session_rows(
        db, HarnessAgentLoopRecord, tenant_id=tenant_id, session_id=session_id
    )
    turn_count = _delete_session_rows(
        db, HarnessTurnRecord, tenant_id=tenant_id, session_id=session_id
    )
    session_lease_count = _delete_session_rows(
        db, HarnessSessionLeaseRecord, tenant_id=tenant_id, session_id=session_id
    )

    return HarnessSessionRecordCleanup(
        session_lease_count=session_lease_count,
        turn_count=turn_count,
        invocation_count=invocation_count,
        run_count=run_count,
        task_frame_count=task_frame_count,
        agent_loop_count=agent_loop_count,
    )


def stage_harness_session_execution_reset(
    db: Session,
    *,
    tenant_id: str,
    session_id: str,
) -> None:
    """Cancel durable Harness execution state while preserving turn receipts.

    与删除同理：这些行只需按条件改状态/删掉，没必要读进 ORM 再逐行处理。
    ``turns`` 用批量 UPDATE 落同一个终态（status/error_json/finished_at/updated_at 四个字段
    的取值与逐行赋值完全一致）。
    """
    now = utc_now()
    db.exec(
        sa_update(HarnessTurnRecord)
        .where(
            HarnessTurnRecord.tenant_id == tenant_id,
            HarnessTurnRecord.session_id == session_id,
            HarnessTurnRecord.status == "started",
        )
        .values(
            status="cancelled",
            error_json={
                "code": "SESSION_RESET",
                "message": "会话已重置，原 Harness turn 已取消。",
            },
            finished_at=now,
            updated_at=now,
        )
    )
    db.exec(
        sa_delete(HarnessInvocationRecord).where(
            HarnessInvocationRecord.tenant_id == tenant_id,
            HarnessInvocationRecord.session_id == session_id,
            HarnessInvocationRecord.status == "started",
        )
    )
    db.exec(
        sa_delete(HarnessRunRecord).where(
            HarnessRunRecord.tenant_id == tenant_id,
            HarnessRunRecord.session_id == session_id,
            HarnessRunRecord.status == "running",
        )
    )
    db.exec(
        sa_delete(HarnessTaskFrameRecord).where(
            HarnessTaskFrameRecord.tenant_id == tenant_id,
            HarnessTaskFrameRecord.session_id == session_id,
            HarnessTaskFrameRecord.status.notin_({"completed", "cancelled", "failed"}),
        )
    )
    db.exec(
        sa_delete(HarnessSessionLeaseRecord).where(
            HarnessSessionLeaseRecord.tenant_id == tenant_id,
            HarnessSessionLeaseRecord.session_id == session_id,
        )
    )


def harness_path_segment(value: str) -> str:
    """Map an external identifier to the exact Harness workspace segment."""

    raw = str(value or "")
    normalized = "".join(
        character
        for character in raw
        if character.isalnum() or character in {"-", "_"}
    )
    prefix = normalized[:72] or "unknown"
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{suffix}"


def harness_storage_root(*, tenant_id: str, db: Session | None = None) -> Path:
    """Resolve the administrator-selected root for new non-sandboxed workspaces."""

    default_root = paths.user_data_dir().resolve() / "harness_workspaces"
    if db is not None:
        row = db.get(UIConfig, tenant_id)
        configured = str(getattr(row, "harness_storage_path", "") or "").strip()
        if row is not None and not bool(getattr(row, "sandbox_enabled", False)) and configured:
            return Path(configured).expanduser().resolve()
    return default_root


def harness_session_workspace_path(
    *, tenant_id: str, session_id: str, db: Session | None = None
) -> Path:
    return (
        harness_storage_root(tenant_id=tenant_id, db=db)
        / harness_path_segment(tenant_id)
        / harness_path_segment(session_id)
    )


def harness_task_workspace_path(
    *,
    tenant_id: str,
    session_id: str,
    task_frame_id: str,
    db: Session | None = None,
) -> Path:
    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        db=db,
    )
    task_path = session_path / harness_path_segment(task_frame_id)
    for parent in (
        session_path.parents[1],
        session_path.parent,
        session_path,
        task_path,
    ):
        if parent.is_symlink():
            raise OSError(
                "refusing to provision Harness workspace through a symlink"
            )
    return task_path


def remove_harness_session_workspace(
    *, tenant_id: str, session_id: str, db: Session | None = None
) -> bool:
    """Remove only one exact tenant/session Harness workspace.

    Parent symlinks are rejected so cleanup can never traverse a redirected
    ``harness_workspaces`` or tenant directory. A symlink at the exact session
    path is unlinked without touching its target.
    """

    session_path = harness_session_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        db=db,
    )
    harness_root = session_path.parents[1]
    tenant_path = session_path.parent

    if harness_root.is_symlink() or tenant_path.is_symlink():
        raise OSError("refusing to clean Harness workspace through a symlinked parent")
    if session_path.is_symlink():
        session_path.unlink()
        return True
    if not session_path.exists():
        return False
    if not session_path.is_dir():
        session_path.unlink()
        return True

    shutil.rmtree(session_path)
    return True


__all__ = [
    "HarnessSessionBusy",
    "HarnessSessionLeaseLost",
    "HarnessSessionLeaseStore",
    "HarnessSessionLeaseToken",
    "HarnessSessionRecordCleanup",
    "HarnessTurnClaim",
    "HarnessTurnConflict",
    "HarnessTurnStore",
    "acquire_harness_session",
    "harness_path_segment",
    "harness_session_workspace_path",
    "harness_storage_root",
    "harness_task_workspace_path",
    "release_harness_session",
    "remove_harness_session_workspace",
    "stage_harness_session_execution_reset",
    "stage_harness_session_record_deletion",
]
