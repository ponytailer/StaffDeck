"""定时任务监控 API（管理员专用）。

给「超级管理员 → 任务监控」子页面提供租户级的调度观测能力，对标 rq-dashboard
但按本产品口径组织：既看**业务侧**（全部定时任务 + 执行记录），也看**运行时侧**
（rq worker / 队列健康）。

设计要点：
- 只读接口放在这里；任务操作（立即执行 / 暂停 / 恢复 / 归档）复用
  ``/api/enterprise/scheduled-tasks`` 的既有端点，避免重复实现与权限分叉。
- 全部端点走 ``require_tenant_admin``，非管理员 403。
- 列表查询一律「一次 JOIN / 一次 IN 批量取名字」，不在循环里逐行查库
  （本地 dev 连远程 PG，逐行查询是启动慢的根因）。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import AgentProfile, ScheduledTask, ScheduledTaskRun, User, utc_now
from app.security.permissions import require_tenant_admin
from app.security.tenant import ensure_tenant

enterprise_router = APIRouter(
    prefix="/api/enterprise/scheduled-task-monitor",
    tags=["enterprise:scheduled-task-monitor"],
)

# 执行记录的状态分组（前端筛选用，避免前端硬编码状态集合）
RUN_PENDING_STATUSES = ("queued", "running", "retrying", "needs_input")
RUN_FAILED_STATUSES = ("failed", "skipped")
RUN_FILTER_GROUPS: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "pending": RUN_PENDING_STATUSES,
    "completed": ("succeeded",),
    "failed": RUN_FAILED_STATUSES,
}

_TASK_STATUSES = ("active", "paused", "completed", "archived")
_IN_FLIGHT_STATUSES = ("queued", "running", "retrying")


class MonitorTaskCounts(BaseModel):
    total: int = 0
    active: int = 0
    paused: int = 0
    completed: int = 0
    archived: int = 0


class MonitorRunCounts(BaseModel):
    total: int = 0
    pending: int = 0
    running: int = 0
    queued: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    needs_input: int = 0
    last_24h_total: int = 0
    last_24h_failed: int = 0


class MonitorRuntimeRead(BaseModel):
    backend: str = "poll"
    enabled: bool = False
    queue_name: str = ""
    redis_connected: bool = False
    counts: dict[str, int] = Field(default_factory=dict)
    workers: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class MonitorOverviewRead(BaseModel):
    generated_at: str
    tasks: MonitorTaskCounts
    runs: MonitorRunCounts
    runtime: MonitorRuntimeRead


class MonitorTaskItem(BaseModel):
    id: str
    title: str
    prompt: str = ""
    status: str
    schedule_type: str
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = ""
    agent_id: str
    agent_name: str | None = None
    created_by_user_id: str
    created_by_name: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    run_count: int = 0
    failure_count: int = 0
    in_flight: int = 0
    concurrency_policy: str = "forbid"
    misfire_policy: str = "coalesce"
    updated_at: str | None = None


class MonitorRunItem(BaseModel):
    id: str
    scheduled_task_id: str
    task_title: str | None = None
    task_status: str | None = None
    agent_id: str
    agent_name: str | None = None
    user_id: str
    user_name: str | None = None
    session_id: str | None = None
    scheduled_for: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    result_summary: str | None = None
    error: str | None = None
    created_at: str


@enterprise_router.get("/overview", response_model=MonitorOverviewRead)
def get_monitor_overview(
    tenant_id: str = Query(...),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> MonitorOverviewRead:
    """任务 / 执行 / 运行时三段式总览（管理员）。"""
    ensure_tenant(db, tenant_id)

    task_counts = _task_counts(db, tenant_id)
    run_counts = _run_counts(db, tenant_id)

    from app.scheduled_tasks import rq_dispatch

    runtime = rq_dispatch.runtime_status()
    return MonitorOverviewRead(
        generated_at=utc_now().isoformat(),
        tasks=task_counts,
        runs=run_counts,
        runtime=MonitorRuntimeRead(**runtime),
    )


@enterprise_router.get("/tasks", response_model=list[MonitorTaskItem])
def list_monitor_tasks(
    tenant_id: str = Query(...),
    status: str | None = Query(None, description="active / paused / completed / archived"),
    agent_id: str | None = Query(None),
    q: str | None = Query(None, description="按标题或提示词模糊搜索"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> list[MonitorTaskItem]:
    """租户下全部定时任务（跨员工、跨创建人），附带员工名与创建人名。"""
    ensure_tenant(db, tenant_id)

    conditions: list[Any] = [ScheduledTask.tenant_id == tenant_id]
    if status:
        conditions.append(ScheduledTask.status == status)
    if agent_id:
        conditions.append(ScheduledTask.agent_id == agent_id)
    if q:
        pattern = f"%{q.strip()}%"
        conditions.append(ScheduledTask.title.ilike(pattern) | ScheduledTask.prompt.ilike(pattern))

    rows = db.exec(
        select(ScheduledTask)
        .where(*conditions)
        .order_by(ScheduledTask.updated_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []

    task_ids = [row.id for row in rows]
    agent_names = _agent_names(db, {row.agent_id for row in rows if row.agent_id})
    user_names = _user_names(db, {row.created_by_user_id for row in rows if row.created_by_user_id})
    failure_counts = _grouped_counts(
        db,
        ScheduledTaskRun.scheduled_task_id,
        ScheduledTaskRun.tenant_id == tenant_id,
        ScheduledTaskRun.scheduled_task_id.in_(task_ids),
        ScheduledTaskRun.status.in_(RUN_FAILED_STATUSES),
    )
    in_flight = _grouped_counts(
        db,
        ScheduledTaskRun.scheduled_task_id,
        ScheduledTaskRun.tenant_id == tenant_id,
        ScheduledTaskRun.scheduled_task_id.in_(task_ids),
        ScheduledTaskRun.status.in_(_IN_FLIGHT_STATUSES),
    )

    return [
        MonitorTaskItem(
            id=row.id,
            title=row.title,
            prompt=row.prompt or "",
            status=row.status,
            schedule_type=row.schedule_type,
            schedule=dict(row.schedule_json or {}),
            timezone=row.timezone or "",
            agent_id=row.agent_id,
            agent_name=agent_names.get(row.agent_id),
            created_by_user_id=row.created_by_user_id,
            created_by_name=user_names.get(row.created_by_user_id),
            next_run_at=_iso(row.next_run_at),
            last_run_at=_iso(row.last_run_at),
            last_status=row.last_status,
            run_count=int(row.run_count or 0),
            failure_count=failure_counts.get(row.id, 0),
            in_flight=in_flight.get(row.id, 0),
            concurrency_policy=row.concurrency_policy or "forbid",
            misfire_policy=row.misfire_policy or "coalesce",
            updated_at=_iso(row.updated_at),
        )
        for row in rows
    ]


@enterprise_router.get("/runs", response_model=list[MonitorRunItem])
def list_monitor_runs(
    tenant_id: str = Query(...),
    status: str = Query("all", description="all / pending / completed / failed 或具体状态值"),
    task_id: str | None = Query(None),
    agent_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_session),
) -> list[MonitorRunItem]:
    """租户下全部执行记录（跨员工、跨创建人），附带任务标题与员工/用户名。"""
    ensure_tenant(db, tenant_id)

    conditions: list[Any] = [ScheduledTaskRun.tenant_id == tenant_id]
    group = RUN_FILTER_GROUPS.get(status)
    if group is None and status and status != "all":
        conditions.append(ScheduledTaskRun.status == status)
    elif group is not None:
        conditions.append(ScheduledTaskRun.status.in_(group))
    if task_id:
        conditions.append(ScheduledTaskRun.scheduled_task_id == task_id)
    if agent_id:
        conditions.append(ScheduledTaskRun.agent_id == agent_id)

    rows = db.exec(
        select(ScheduledTaskRun, ScheduledTask)
        .join(ScheduledTask, ScheduledTaskRun.scheduled_task_id == ScheduledTask.id)
        .where(*conditions)
        .order_by(ScheduledTaskRun.created_at.desc())
        .limit(limit)
    ).all()
    if not rows:
        return []

    agent_names = _agent_names(db, {run.agent_id for run, _ in rows if run.agent_id})
    user_names = _user_names(db, {run.user_id for run, _ in rows if run.user_id})

    return [
        MonitorRunItem(
            id=run.id,
            scheduled_task_id=run.scheduled_task_id,
            task_title=task.title if task else None,
            task_status=task.status if task else None,
            agent_id=run.agent_id,
            agent_name=agent_names.get(run.agent_id),
            user_id=run.user_id,
            user_name=user_names.get(run.user_id),
            session_id=run.session_id,
            scheduled_for=run.scheduled_for.isoformat(),
            status=run.status,
            started_at=_iso(run.started_at),
            finished_at=_iso(run.finished_at),
            duration_ms=_duration_ms(run.started_at, run.finished_at),
            result_summary=run.result_summary,
            error=run.error,
            created_at=run.created_at.isoformat(),
        )
        for run, task in rows
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _task_counts(db: Session, tenant_id: str) -> MonitorTaskCounts:
    rows = db.exec(
        select(ScheduledTask.status, func.count())
        .where(ScheduledTask.tenant_id == tenant_id)
        .group_by(ScheduledTask.status)
    ).all()
    by_status = {str(status): int(count) for status, count in rows}
    counts = MonitorTaskCounts(total=sum(by_status.values()))
    for name in _TASK_STATUSES:
        if hasattr(counts, name):
            setattr(counts, name, by_status.get(name, 0))
    return counts


def _run_counts(db: Session, tenant_id: str) -> MonitorRunCounts:
    rows = db.exec(
        select(ScheduledTaskRun.status, func.count())
        .where(ScheduledTaskRun.tenant_id == tenant_id)
        .group_by(ScheduledTaskRun.status)
    ).all()
    by_status = {str(status): int(count) for status, count in rows}

    counts = MonitorRunCounts(total=sum(by_status.values()), **{
        name: by_status.get(name, 0)
        for name in ("running", "queued", "succeeded", "failed", "skipped", "needs_input")
    })
    counts.pending = sum(by_status.get(name, 0) for name in RUN_PENDING_STATUSES)

    last_24h_start = utc_now() - timedelta(hours=24)
    counts.last_24h_total = int(
        db.exec(
            select(func.count())
            .select_from(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.tenant_id == tenant_id,
                ScheduledTaskRun.created_at >= last_24h_start,
            )
        ).one()
        or 0
    )
    counts.last_24h_failed = int(
        db.exec(
            select(func.count())
            .select_from(ScheduledTaskRun)
            .where(
                ScheduledTaskRun.tenant_id == tenant_id,
                ScheduledTaskRun.created_at >= last_24h_start,
                ScheduledTaskRun.status.in_(RUN_FAILED_STATUSES),
            )
        ).one()
        or 0
    )
    return counts


def _grouped_counts(db: Session, key_column: Any, *conditions: Any) -> dict[str, int]:
    rows = db.exec(
        select(key_column, func.count()).where(*conditions).group_by(key_column)
    ).all()
    return {str(key): int(count) for key, count in rows}


def _agent_names(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.exec(
        select(AgentProfile.id, AgentProfile.name).where(AgentProfile.id.in_(ids))
    ).all()
    return {str(row[0]): str(row[1]) for row in rows if row[1]}


def _user_names(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.exec(
        select(User.id, User.username, User.display_name).where(User.id.in_(ids))
    ).all()
    return {str(row[0]): str(row[2] or row[1]) for row in rows if (row[2] or row[1])}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:  # pragma: no cover
        return str(value)


def _duration_ms(started: Any, finished: Any) -> int | None:
    if started is None or finished is None:
        return None
    try:
        return max(0, int((finished - started).total_seconds() * 1000))
    except TypeError:  # pragma: no cover - 时区不一致等异常情况
        return None
