from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api import scheduled_task_monitor as mon
from app.db.models import AgentProfile, ScheduledTask, ScheduledTaskRun, Tenant, User, utc_now
from app.scheduled_tasks import rq_dispatch
from app.security.permissions import require_tenant_admin

TENANT = "tenant_demo"


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _seed(engine) -> None:
    now = utc_now()
    with Session(engine) as db:
        db.add(Tenant(id=TENANT, name="演示租户"))
        db.add(User(id="user_admin", tenant_id=TENANT, username="admin",
                    display_name="超级管理员", role="admin", password_hash="x"))
        db.add(User(id="user_member", tenant_id=TENANT, username="zhangsan",
                    display_name="张三", role="member", password_hash="x"))
        db.add(AgentProfile(id="agent_ops", tenant_id=TENANT, name="运维助手"))
        db.add(ScheduledTask(id="task_a", tenant_id=TENANT, agent_id="agent_ops",
                             created_by_user_id="user_member", title="每日巡检", prompt="检查集群",
                             schedule_type="daily", schedule_json={"time": "09:00"},
                             status="active", next_run_at=now + timedelta(hours=1), run_count=3))
        db.add(ScheduledTask(id="task_b", tenant_id=TENANT, agent_id="agent_ops",
                             created_by_user_id="user_admin", title="周报汇总", prompt="汇总周报",
                             schedule_type="weekly", schedule_json={"time": "18:00", "weekdays": [4]},
                             status="paused", run_count=1))
        db.add(ScheduledTaskRun(id="run_1", tenant_id=TENANT, scheduled_task_id="task_a",
                                agent_id="agent_ops", user_id="user_member",
                                scheduled_for=now - timedelta(hours=2), status="failed",
                                started_at=now - timedelta(hours=2),
                                finished_at=now - timedelta(hours=2) + timedelta(seconds=90),
                                created_at=now - timedelta(hours=2),
                                error="超时"))
        db.add(ScheduledTaskRun(id="run_2", tenant_id=TENANT, scheduled_task_id="task_a",
                                agent_id="agent_ops", user_id="user_member",
                                scheduled_for=now - timedelta(hours=1), status="running",
                                started_at=now - timedelta(minutes=5),
                                created_at=now - timedelta(hours=1)))
        db.add(ScheduledTaskRun(id="run_3", tenant_id=TENANT, scheduled_task_id="task_b",
                                agent_id="agent_ops", user_id="user_admin",
                                scheduled_for=now - timedelta(days=8), status="succeeded",
                                started_at=now - timedelta(days=8),
                                finished_at=now - timedelta(days=8) + timedelta(seconds=15),
                                created_at=now - timedelta(days=8)))
        db.commit()


ADMIN = User(id="user_admin", tenant_id=TENANT, username="admin", role="admin", password_hash="x")
MEMBER = User(id="user_member", tenant_id=TENANT, username="zhangsan", role="member", password_hash="x")


def _list_tasks(db, **overrides):
    """直调端点函数时补全 Query 默认值（TestClient 才负责解析未传参数）。"""
    params = {
        "tenant_id": TENANT,
        "status": None,
        "agent_id": None,
        "q": None,
        "limit": 200,
        "current_user": ADMIN,
        "db": db,
    }
    params.update(overrides)
    return mon.list_monitor_tasks(**params)


def _list_runs(db, **overrides):
    params = {
        "tenant_id": TENANT,
        "status": "all",
        "task_id": None,
        "agent_id": None,
        "limit": 200,
        "current_user": ADMIN,
        "db": db,
    }
    params.update(overrides)
    return mon.list_monitor_runs(**params)


@pytest.fixture
def stub_runtime(monkeypatch):
    """监控接口不应在单测里碰真实 Redis（仅 API 类用例显式声明）。"""
    monkeypatch.setattr(
        rq_dispatch,
        "runtime_status",
        lambda: {
            "backend": "rq",
            "enabled": True,
            "queue_name": "scheduled_tasks",
            "redis_connected": True,
            "counts": {"queued": 0, "scheduled": 2, "started": 1,
                       "failed": 0, "finished": 5, "deferred": 0},
            "workers": [],
            "error": None,
        },
    )


def test_overview_aggregates_task_run_and_runtime(stub_runtime) -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        res = mon.get_monitor_overview(tenant_id=TENANT, current_user=ADMIN, db=db)

    assert res.tasks.total == 2
    assert res.tasks.active == 1
    assert res.tasks.paused == 1
    assert res.runs.total == 3
    assert res.runs.failed == 1
    assert res.runs.running == 1
    assert res.runs.succeeded == 1
    # pending 是 queued/running/retrying/needs_input 之和
    assert res.runs.pending == 1
    # 只有 2 条是 24h 内创建的
    assert res.runs.last_24h_total == 2
    assert res.runs.last_24h_failed == 1
    assert res.runtime.redis_connected is True
    assert res.runtime.counts["scheduled"] == 2


def test_tasks_enriched_with_names_and_counters(stub_runtime) -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        rows = _list_tasks(db)

    by_id = {row.id: row for row in rows}
    assert set(by_id) == {"task_a", "task_b"}
    assert by_id["task_a"].agent_name == "运维助手"
    assert by_id["task_a"].created_by_name == "张三"
    assert by_id["task_a"].failure_count == 1
    assert by_id["task_a"].in_flight == 1
    assert by_id["task_b"].failure_count == 0
    assert by_id["task_b"].in_flight == 0


def test_tasks_filter_by_status_and_query(stub_runtime) -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        paused = _list_tasks(db, status="paused")
        assert [row.id for row in paused] == ["task_b"]

        hit = _list_tasks(db, q="巡检")
        assert [row.id for row in hit] == ["task_a"]

        miss = _list_tasks(db, q="不存在的关键字")
        assert miss == []


def test_runs_include_names_duration_and_group_filter(stub_runtime) -> None:
    engine = _test_engine()
    _seed(engine)
    with Session(engine) as db:
        rows = _list_runs(db)
        by_id = {row.id: row for row in rows}
        assert by_id["run_1"].task_title == "每日巡检"
        assert by_id["run_1"].agent_name == "运维助手"
        assert by_id["run_1"].user_name == "张三"
        assert by_id["run_1"].duration_ms == 90_000
        # 运行中的记录没有完成时间 → 时长为 None
        assert by_id["run_2"].duration_ms is None

        pending = _list_runs(db, status="pending")
        assert [row.id for row in pending] == ["run_2"]

        failed = _list_runs(db, status="failed")
        assert [row.id for row in failed] == ["run_1"]

        by_task = _list_runs(db, task_id="task_b")
        assert [row.id for row in by_task] == ["run_3"]


def test_monitor_endpoints_are_admin_only() -> None:
    with pytest.raises(HTTPException) as exc:
        require_tenant_admin(tenant_id=TENANT, current_user=MEMBER)
    assert exc.value.status_code == 403
    # 管理员放行
    assert require_tenant_admin(tenant_id=TENANT, current_user=ADMIN) is ADMIN


def test_runtime_status_degrades_when_redis_unavailable(monkeypatch) -> None:
    # 监控走 inspect 档连接；语义不变：拿不到连接即降级
    monkeypatch.setattr(rq_dispatch, "_redis_connection", lambda *args, **kwargs: None)

    status = rq_dispatch.runtime_status()
    assert status["enabled"] is True
    assert status["redis_connected"] is False
    assert status["counts"]["queued"] == 0
    assert status["error"]


def test_runtime_status_reports_poll_backend(monkeypatch) -> None:
    class _Settings:
        scheduler_backend = "poll"
        scheduled_task_queue = "scheduled_tasks"

    monkeypatch.setattr(rq_dispatch, "get_settings", lambda: _Settings())

    status = rq_dispatch.runtime_status()
    assert status["enabled"] is False
    assert status["backend"] == "poll"
    assert "poll" in status["error"]
