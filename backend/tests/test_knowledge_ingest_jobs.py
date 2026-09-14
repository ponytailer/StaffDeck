"""知识库文档入库的调度层测试（不需要 Redis / rq / 真库）。

重点验证两件事：
1. 任务函数必须能被 **rq 自己的解析器** 解析（否则 worker 侧抛
   ``Invalid attribute name``，任务只是排进 Redis 变哑弹）；
2. 调度必须 rq 优先、Redis 不可用时降级进程内队列、两条通道都挂时返回 False
   而不是抛异常——上传接口绝不能因为后台队列不可用而 500。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.knowledge.ingest_jobs import (
    JOB_FUNC_PATH,
    JOB_NAME,
    run_ingest_job,
    schedule_knowledge_ingest,
)


def test_job_func_path_is_importable() -> None:
    """必须能被 rq 自己的解析器取到，而不是仅仅「长得像点分」。"""

    from rq.utils import import_attribute

    target = import_attribute(JOB_FUNC_PATH)
    assert callable(target)
    assert target.__name__ == "run_ingest_job"


def test_job_func_is_module_level_so_it_can_be_persisted() -> None:
    """入库曾用 ``service.run_ingest_job``（bound method）入队。

    bound method 既不能被 rq 跨进程解析，也写不进进程内队列的 Redis 重启恢复
    记录（``_serialize_callable`` 直接返回 None）——结果是「服务重启后入库任务
    静默丢失」。这条用例把「必须是模块级函数」钉住。
    """

    import app.knowledge.ingest_jobs as module
    from app.async_jobs import _serialize_callable
    from app.knowledge.service import KnowledgeService

    assert _serialize_callable(KnowledgeService(None).run_ingest_job) is None
    assert _serialize_callable(module.run_ingest_job) == "app.knowledge.ingest_jobs:run_ingest_job"
    assert _serialize_callable(run_ingest_job) is not None


def test_schedule_prefers_rq(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []
    kwargs_seen: list[dict[str, Any]] = []

    def _fake_enqueue(queue_name: str, func_path: str, *args: Any, **kwargs: Any) -> str:
        calls.append((queue_name, func_path, *args))
        kwargs_seen.append(kwargs)
        return "rq-job-1"

    def _should_not_be_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rq 可用时不应触碰进程内队列")

    monkeypatch.setattr("app.scheduled_tasks.rq_dispatch.enqueue_job", _fake_enqueue)
    monkeypatch.setattr("app.async_jobs.enqueue_async_job", _should_not_be_called)

    assert schedule_knowledge_ingest("kjob_1", tenant_id="tenant_a", filename="a.pdf") is True
    settings = get_settings()
    assert calls == [(settings.knowledge_ingest_queue, JOB_FUNC_PATH, "kjob_1")]
    assert kwargs_seen[0]["timeout_seconds"] == settings.knowledge_ingest_job_timeout_seconds


def test_ingest_job_timeout_exceeds_worst_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """job 超时必须明显大于正常耗时。

    上限小于正常耗时会把「跑得慢」变成「跑失败」：rq worker 到点直接 kill 掉一个
    其实仍在正常推进的入库任务。入库的 LLM 两阶段最坏 ~330s，解析/切片再留足余量。
    """

    settings = get_settings()
    assert settings.knowledge_ingest_job_timeout_seconds >= 900
    # 也要大于定时任务队列的默认上限，否则单独起 worker 时会更容易被误杀
    assert settings.knowledge_ingest_job_timeout_seconds > settings.scheduled_task_job_timeout_seconds


def test_schedule_falls_back_to_inprocess_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.scheduled_tasks.rq_dispatch.enqueue_job",
        lambda *args, **kwargs: None,
    )
    captured: dict[str, Any] = {}

    class _Job:
        id = "local-job-1"

    def _fake_async_enqueue(name: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        captured["name"] = name
        captured["func"] = func
        captured["args"] = args
        captured["metadata"] = kwargs.get("metadata")
        return _Job()

    monkeypatch.setattr("app.async_jobs.enqueue_async_job", _fake_async_enqueue)

    assert schedule_knowledge_ingest("kjob_2", tenant_id="tenant_a", filename="b.md") is True
    assert captured["name"] == JOB_NAME
    assert captured["args"] == ("kjob_2",)
    # 两条通道必须调用同一个执行函数，否则语义会分叉
    assert captured["func"] is run_ingest_job
    assert captured["metadata"] == {"tenant_id": "tenant_a", "filename": "b.md"}


def test_schedule_survives_rq_broken(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("rq exploded")

    class _Job:
        id = "local-job-2"

    monkeypatch.setattr("app.scheduled_tasks.rq_dispatch.enqueue_job", _boom)
    monkeypatch.setattr("app.async_jobs.enqueue_async_job", lambda *a, **kw: _Job())
    assert schedule_knowledge_ingest("kjob_3") is True


def test_schedule_returns_false_when_everything_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.scheduled_tasks.rq_dispatch.enqueue_job",
        lambda *args, **kwargs: None,
    )

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("queue closed")

    monkeypatch.setattr("app.async_jobs.enqueue_async_job", _boom)
    # 后台调度失败不该让上传接口 500：返回 False，job 留在 PG 里可重试 / 可取消
    assert schedule_knowledge_ingest("kjob_4") is False


def test_run_ingest_job_is_safe_for_missing_job() -> None:
    """worker 拿到已被删掉 / 不存在的 job_id 时必须是安全 no-op。"""

    run_ingest_job("kjob_definitely_missing")
