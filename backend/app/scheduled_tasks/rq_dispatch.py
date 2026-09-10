"""定时任务 rq 调度适配层（独立模块，便于启停 / 回退）。

架构：PG 主源 + Redis 仅存「执行触发」
- ``scheduled_tasks`` 表始终是唯一事实来源：任务配置、``next_run_at``、状态都在 PG；
- Redis 只存「什么时候触发哪个 ``task_id``」，由 rq-scheduler 管理，可随时从 PG 重建；
- 写路径（create / update / archive）调用 :func:`sync_task` 做增量同步；
- 启动路径调用 :func:`sync_all_on_startup` 做全量重建（Redis 重启 / 清空后自愈）；
- 执行由独立 rq worker 进程消费触发项，调用 :func:`run_task` 回读 PG 最新配置后执行。

本模块是所有 rq 依赖的唯一入口：``main.py`` 与 ``api`` 只调这里的函数，不直接 import rq。
把 ``settings.scheduler_backend`` 设为 ``"poll"`` 即可整体切回旧的进程内轮询实现
（``app.scheduled_tasks.worker``），业务代码零改动。

优雅降级：``redis_host`` 未配置 / 连接失败 / ``scheduler_backend != "rq"`` 时，
所有写函数静默 no-op 并返回 False，不影响任务本身写入 PG 与手动 ``/run-now`` 执行。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.db.models import ScheduledTask, utc_now

logger = logging.getLogger(__name__)

# 触发时由 worker 回读 PG 执行；字符串路径保证跨进程可导入（rq 用 pickle 序列化）。
_TASK_FUNC = "app.scheduled_tasks.rq_dispatch.run_task"

# 进程级单例（redis-py 自带连接池，复用实例避免重复建连）
# 三类用途的读超时需求互斥，必须分开缓存，见 _redis_connection 的说明。
_connections: dict[str, Any] = {}
_connection_disabled = False
_scheduler: Any | None = None

CONNECTION_MODES = ("control", "blocking", "inspect")


def _redis_connection(mode: str = "control") -> Any | None:
    """rq 专用 Redis 连接，按用途分档（读超时需求互斥）。

    - ``control``（默认）：控制面写路径（``sync_task`` / ``cancel_task`` /
      管理端调度器）。1.5s 读超时——这些都是非阻塞命令，Redis 卡住时快速失败，
      不拖慢 API 请求。
    - ``blocking``：worker 进程的阻塞出队。**必须不带 1.5s 读超时**：rq 用
      ``BLMOVE`` 长时间阻塞等任务（``dequeue_timeout ≈ worker_ttl - 15`` 秒），
      读超时一到就抛 ``redis.exceptions.TimeoutError``，被 ``Worker.work()``
      捕获后直接 break —— 表现就是「worker 启动十几秒后自动退出」。
    - ``inspect``：管理端只读监控。rq 构造 ``Worker`` 时 ``_set_connection()``
      会把连接池的 ``socket_timeout`` 改写成 ``dequeue_timeout + 10``（rq 自己的
      兜底），单独一档，避免污染 control 连接的超时配置。

    注意：**不能**复用 ``app.redis_client.get_redis()``——那个实例是
    ``decode_responses=True``，而 rq 用 pickle 序列化 job，必须是 bytes 连接。
    """
    global _connection_disabled
    if _connection_disabled:
        return None
    if mode not in CONNECTION_MODES:
        raise ValueError(f"未知的 rq Redis 连接模式：{mode}")
    cached = _connections.get(mode)
    if cached is not None:
        return cached

    settings = get_settings()
    if not settings.redis_host:
        _connection_disabled = True
        return None

    try:
        import redis  # 延迟导入：未安装 redis 包时整体降级
    except ImportError:  # pragma: no cover - 依赖缺失属环境问题
        logger.warning("未安装 redis 包，rq 定时调度禁用")
        _connection_disabled = True
        return None

    try:
        conn = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            socket_connect_timeout=1.5,
            # 阻塞档交给 rq 自己决定超时（见下方 disconnect），其余档保持短超时
            socket_timeout=None if mode == "blocking" else 1.5,
            socket_keepalive=True,  # 长阻塞连接靠内核 keepalive 防中间设备判死
            decode_responses=False,
        )
        conn.ping()
        if mode == "blocking":
            # ping 用掉的这条连接没有读超时；丢掉它，后续连接会按 rq 构造 Worker
            # 时写入连接池的 socket_timeout（dequeue_timeout + 10）建立：
            # 既不会被短超时打断阻塞读，又保留 rq 对「连接真的僵死」的兜底退出。
            conn.connection_pool.disconnect()
    except Exception:
        logger.warning(
            "rq 定时调度：Redis 连接失败（%s:%s），调度禁用，任务仅支持手动执行",
            settings.redis_host,
            settings.redis_port,
        )
        _connection_disabled = True
        return None

    _connections[mode] = conn
    return conn


def get_scheduler() -> Any | None:
    """返回 rq-scheduler 实例；后端非 rq 或 Redis 不可用时返回 None。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    settings = get_settings()
    if settings.scheduler_backend != "rq":
        return None

    conn = _redis_connection()
    if conn is None:
        return None

    try:
        from rq_scheduler import Scheduler
    except ImportError:  # pragma: no cover - 依赖缺失属环境问题
        logger.warning("未安装 rq-scheduler，rq 定时调度禁用")
        return None

    try:
        _scheduler = Scheduler(queue_name=settings.scheduled_task_queue, connection=conn)
    except Exception:  # noqa: BLE001
        logger.warning("rq-scheduler 初始化失败，调度禁用", exc_info=True)
        return None
    return _scheduler


def reset_caches() -> None:
    """仅测试使用：清空连接 / scheduler 单例缓存。"""
    global _connection_disabled, _scheduler
    _connections.clear()
    _connection_disabled = False
    _scheduler = None


def _cancel_job(scheduler: Any, task_id: str) -> None:
    # cancel 接受 job_id 字符串（zrem），不存在时也是 no-op
    try:
        scheduler.cancel(task_id)
    except Exception:  # noqa: BLE001 - 取消失败不应影响主流程
        logger.debug("rq cancel 忽略异常 task=%s", task_id, exc_info=True)


def sync_task(task: ScheduledTask) -> bool:
    """把一个任务同步到 rq 调度。

    - ``status == "active"`` 且有 ``next_run_at`` → 注册 / 覆盖触发 job；
    - 其它情况（paused / completed / archived / 无下次时间）→ 取消已注册 job。

    固定 ``job_id = task.id``，重复调用为覆盖，天然幂等。
    ``next_run_at`` 若已过期，rq 会立即触发，是否真正执行由
    ``execute_scheduled_task`` 内的 misfire 策略决定（此处不重复判断）。
    """
    scheduler = get_scheduler()
    if scheduler is None:
        return False

    if task.status != "active" or task.next_run_at is None:
        _cancel_job(scheduler, task.id)
        return True

    settings = get_settings()
    try:
        scheduler.enqueue_at(
            task.next_run_at,  # naive UTC（PG 存法）；to_unix 按 UTC 处理，语义一致
            _TASK_FUNC,
            task.id,
            task.next_run_at.isoformat(),
            False,
            job_id=task.id,
            timeout=settings.scheduled_task_job_timeout_seconds,
            job_result_ttl=600,
        )
    except Exception:  # noqa: BLE001
        logger.warning("rq 注册任务失败 task=%s", task.id, exc_info=True)
        return False
    return True


def cancel_task(task_id: str) -> bool:
    """取消某任务已注册的触发 job（删除 / 归档 / 暂停时调用）。"""
    scheduler = get_scheduler()
    if scheduler is None:
        return False
    _cancel_job(scheduler, task_id)
    return True


def sync_all_on_startup(db: Session) -> int:
    """启动时按 PG 全量重建 rq 调度（Redis 重启 / 清空后自愈）。

    只注册、不执行：过期的 ``next_run_at`` 会被 rq 立即触发，是否真正执行由
    ``execute_scheduled_task`` 内的 misfire 策略（coalesce / skip）决定。
    """
    scheduler = get_scheduler()
    if scheduler is None:
        return 0

    now = utc_now()
    rows = db.exec(select(ScheduledTask).where(ScheduledTask.status == "active")).all()
    synced = 0
    for task in rows:
        if task.next_run_at is None:
            continue
        if task.end_at is not None and task.end_at < now:
            continue
        if sync_task(task):
            synced += 1
    logger.info("rq 定时调度启动同步完成：%s 个活动任务", synced)
    return synced


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:  # pragma: no cover - 非 datetime 时原样返回
        return str(value)


def _worker_summary(worker: Any) -> dict[str, Any]:
    try:
        state = worker.get_state()
    except Exception:  # noqa: BLE001 - 单个 worker 异常不拖垮整体
        state = "unknown"

    current_job: dict[str, Any] | None = None
    try:
        job = worker.get_current_job()
    except Exception:  # noqa: BLE001
        job = None
    if job is not None:
        task_id = job.args[0] if getattr(job, "args", None) else None
        current_job = {
            "id": job.id,
            "task_id": task_id,
            "started_at": _iso(getattr(job, "started_at", None)),
        }

    queues = getattr(worker, "queues", None) or []
    return {
        "name": getattr(worker, "name", "") or "",
        "hostname": getattr(worker, "hostname", "") or "",
        "pid": getattr(worker, "pid", None),
        "queues": [getattr(q, "name", str(q)) for q in queues],
        "state": state,
        "last_heartbeat": _iso(getattr(worker, "last_heartbeat", None)),
        "current_job": current_job,
    }


def runtime_status() -> dict[str, Any]:
    """rq 运行时快照，供管理员监控页展示。

    返回结构（任何情况下都可 JSON 序列化，绝不抛异常）::

        {
          "backend": "rq",            # 当前调度后端开关值
          "enabled": true,            # 是否启用 rq
          "queue_name": "scheduled_tasks",
          "redis_connected": true,
          "counts": {"queued", "scheduled", "started", "failed", "finished", "deferred"},
          "workers": [{name, hostname, pid, queues, state, last_heartbeat, current_job}],
          "error": null
        }

    - ``scheduled``：rq-scheduler 里等待触发的 job 数（= 活跃任务的已注册触发项）；
    - ``queued``：已到期、等待 worker 领取的 job 数；
    - ``started`` / ``failed`` / ``finished`` / ``deferred``：rq registry 计数。

    后端为 ``poll`` 或 Redis 不可用时照常返回，仅把 ``enabled`` /
    ``redis_connected`` 置 False 并在 ``error`` 里说明——监控页据此提示。
    """
    settings = get_settings()
    status: dict[str, Any] = {
        "backend": settings.scheduler_backend,
        "enabled": settings.scheduler_backend == "rq",
        "queue_name": settings.scheduled_task_queue,
        "redis_connected": False,
        "counts": {
            "queued": 0,
            "scheduled": 0,
            "started": 0,
            "failed": 0,
            "finished": 0,
            "deferred": 0,
        },
        "workers": [],
        "error": None,
    }

    if settings.scheduler_backend != "rq":
        status["error"] = "当前使用进程内轮询调度（SCHEDULER_BACKEND=poll），未启用 rq 运行时监控"
        return status

    conn = _redis_connection("inspect")
    if conn is None:
        status["error"] = "Redis 未配置或连接失败，rq 调度已降级为仅手动执行"
        return status

    status["redis_connected"] = True
    try:
        from rq import Queue, Worker

        queue = Queue(settings.scheduled_task_queue, connection=conn)
        status["counts"]["queued"] = int(queue.count)
        status["counts"]["started"] = int(queue.started_job_registry.count)
        status["counts"]["failed"] = int(queue.failed_job_registry.count)
        status["counts"]["finished"] = int(queue.finished_job_registry.count)
        status["counts"]["deferred"] = int(queue.deferred_job_registry.count)

        scheduler = get_scheduler()
        if scheduler is not None:
            status["counts"]["scheduled"] = int(scheduler.count())

        status["workers"] = [_worker_summary(worker) for worker in Worker.all(connection=conn)]
    except Exception as exc:  # noqa: BLE001 - 监控读取失败不影响主流程
        logger.warning("读取 rq 运行时状态失败", exc_info=True)
        status["error"] = f"读取 rq 运行时状态失败：{exc}"
    return status


def run_task(task_id: str, scheduled_for: str | None = None, manual: bool = False) -> str:
    """rq worker 消费入口：回读 PG 最新配置后执行，并重新注册下一次。

    与旧轮询实现共用同一套业务执行（``execute_scheduled_task``），
    包括并发策略、misfire 策略、Harness 结果反推，均无改动。
    """
    from app.scheduled_tasks.service import execute_scheduled_task  # 局部导入避免环

    moment: datetime | None = None
    if scheduled_for:
        try:
            moment = datetime.fromisoformat(scheduled_for)
        except ValueError:
            moment = None

    with Session(engine) as db:
        task = db.get(ScheduledTask, task_id)
        if task is None:
            logger.warning("rq 执行跳过：任务不存在 task=%s", task_id)
            return "missing"
        if task.status != "active" and not manual:
            logger.info("rq 执行跳过：任务非 active task=%s status=%s", task_id, task.status)
            cancel_task(task_id)
            return "inactive"

        run = execute_scheduled_task(db, task, scheduled_for=moment, manual=manual)
        db.refresh(task)

        # 执行完 execute_scheduled_task 已按业务推进 PG 的 next_run_at，
        # 这里据此重注册下一次触发 job（reschedule 失败不影响已完成的结果落库）。
        if task.status == "active" and task.next_run_at is not None:
            sync_task(task)
        else:
            cancel_task(task_id)
        return run.status
