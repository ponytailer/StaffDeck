from __future__ import annotations

from datetime import timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.scheduled_tasks import rq_dispatch
from app.scheduled_tasks import service as scheduled_service
from app.db.models import ScheduledTask, utc_now


class _FakeScheduler:
    """记录 enqueue_at / cancel 调用，替代真实 Redis 调度器。"""

    def __init__(self) -> None:
        self.enqueued: list[dict] = []
        self.cancelled: list[str] = []

    def enqueue_at(self, when, func, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        self.enqueued.append({"when": when, "func": func, "args": args, "kwargs": kwargs})
        return object()

    def cancel(self, job):  # noqa: ANN001
        self.cancelled.append(job)


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_task(engine, **overrides) -> ScheduledTask:
    now = utc_now()
    values = {
        "id": "sched_test1",
        "tenant_id": "tenant_demo",
        "agent_id": "agent_demo",
        "created_by_user_id": "user_demo",
        "title": "测试任务",
        "prompt": "执行点检",
        "schedule_type": "daily",
        "status": "active",
        "next_run_at": now + timedelta(hours=1),
    }
    values.update(overrides)
    task = ScheduledTask(**values)
    with Session(engine) as db:
        db.add(task)
        db.commit()
        db.refresh(task)
        db.expunge(task)
    return task


def test_sync_task_registers_job_with_stable_id(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    task = _make_task(_test_engine())
    assert rq_dispatch.sync_task(task) is True

    assert len(fake.enqueued) == 1
    call = fake.enqueued[0]
    # 固定 job_id = task.id，保证幂等覆盖
    assert call["kwargs"]["job_id"] == task.id
    assert call["func"] == rq_dispatch._TASK_FUNC
    assert call["args"][0] == task.id
    assert call["args"][1] == task.next_run_at.isoformat()
    assert call["args"][2] is False
    assert call["when"] == task.next_run_at


def test_sync_task_cancels_when_not_active(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    paused = _make_task(_test_engine(), id="sched_paused", status="paused", next_run_at=None)
    assert rq_dispatch.sync_task(paused) is True
    assert fake.enqueued == []
    assert fake.cancelled == ["sched_paused"]


def test_sync_task_noop_when_scheduler_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: None)
    task = _make_task(_test_engine(), id="sched_off")
    # 后端不可用时静默降级，不抛异常
    assert rq_dispatch.sync_task(task) is False
    assert rq_dispatch.cancel_task("sched_off") is False


def test_sync_all_on_startup_registers_active_within_window(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    engine = _test_engine()
    now = utc_now()
    _make_task(engine, id="sched_active", next_run_at=now + timedelta(hours=2))
    _make_task(engine, id="sched_past", next_run_at=now - timedelta(minutes=5))  # 过期仍注册
    _make_task(engine, id="sched_paused", status="paused", next_run_at=None)
    _make_task(engine, id="sched_ended", status="active",
               next_run_at=now + timedelta(hours=1), end_at=now - timedelta(days=1))

    with Session(engine) as db:
        assert rq_dispatch.sync_all_on_startup(db) == 2

    ids = {c["kwargs"]["job_id"] for c in fake.enqueued}
    assert ids == {"sched_active", "sched_past"}  # 过期任务也注册，交由 misfire 策略判定


def test_run_task_reschedules_after_execution(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    engine = _test_engine()
    monkeypatch.setattr(rq_dispatch, "engine", engine)
    task = _make_task(engine, id="sched_run")
    next_after = task.next_run_at + timedelta(days=1)

    def _fake_execute(db, row, *, scheduled_for=None, manual=False):  # noqa: ANN001
        row.next_run_at = next_after
        row.status = "active"
        db.add(row)
        db.commit()

        class _Run:
            status = "succeeded"

        return _Run()

    monkeypatch.setattr(scheduled_service, "execute_scheduled_task", _fake_execute)

    assert rq_dispatch.run_task(task.id, task.next_run_at.isoformat(), False) == "succeeded"
    # 执行后按 PG 新 next_run_at 重注册下一次
    assert len(fake.enqueued) == 1
    assert fake.enqueued[0]["when"] == next_after
    assert fake.cancelled == []


def test_run_task_cancels_when_task_completes(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    engine = _test_engine()
    monkeypatch.setattr(rq_dispatch, "engine", engine)
    task = _make_task(engine, id="sched_done")

    def _fake_execute(db, row, *, scheduled_for=None, manual=False):  # noqa: ANN001
        row.status = "completed"
        row.next_run_at = None
        db.add(row)
        db.commit()

        class _Run:
            status = "succeeded"

        return _Run()

    monkeypatch.setattr(scheduled_service, "execute_scheduled_task", _fake_execute)

    rq_dispatch.run_task(task.id, None, False)
    assert fake.enqueued == []
    assert fake.cancelled == ["sched_done"]


def test_run_task_skips_inactive_without_manual(monkeypatch) -> None:
    fake = _FakeScheduler()
    monkeypatch.setattr(rq_dispatch, "get_scheduler", lambda: fake)

    engine = _test_engine()
    monkeypatch.setattr(rq_dispatch, "engine", engine)
    task = _make_task(engine, id="sched_archived", status="archived", next_run_at=None)

    called = {"n": 0}

    def _fake_execute(*args, **kwargs):  # noqa: ANN002, ANN003
        called["n"] += 1

    monkeypatch.setattr(scheduled_service, "execute_scheduled_task", _fake_execute)

    assert rq_dispatch.run_task(task.id, None, False) == "inactive"
    assert called["n"] == 0
    assert fake.cancelled == ["sched_archived"]
