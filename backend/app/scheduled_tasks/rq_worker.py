"""定时任务独立 rq worker 进程入口。

用法::

    python -m app.scheduled_tasks.rq_worker

进程职责：
1. 初始化 DB 与种子数据（与 API 进程同一套 ``DATABASE_URL``）；
2. 后台线程运行 rq Scheduler：把到期的 scheduled job 从 Redis 有序集移入执行队列；
3. 主线程运行 rq Worker：消费 :func:`rq_dispatch.consume_queue_names` 给出的全部
   队列——定时任务触发（``scheduled_tasks``）+ SOP 边条件离线编译
   （``skill_compile``）等通用后台工作。

与 FastAPI（``single_port_app``）进程互相独立：API 只负责写时同步 / 启动全量同步，
本进程只负责按点触发与执行，二者共享同一个 Redis 与 PG。

扩容：rq 单 worker 同时只处理一个 job，需要并发就多起几个本进程（共享同一队列）。
需要把编译工作与定时任务隔离时，可以另起一个进程并让 ``Worker`` 只监听
``skill_compile`` 队列。
"""

from __future__ import annotations

import logging
import sys
import threading

from sqlmodel import Session
from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.config import get_settings


def _init_runtime() -> None:
    init_db()
    with Session(engine) as db:
        seed_demo_data(db)


def _start_scheduler(scheduler: object) -> None:
    # rq-scheduler 的 run() 会安装 SIGINT/SIGTERM 处理器，而 signal 只能在主线程
    # 注册；这里在子线程运行调度循环，故屏蔽其信号安装（进程退出由主线程的
    # rq Worker 处理，调度线程为 daemon 随进程结束）。
    try:
        setattr(scheduler, "_install_signal_handlers", lambda: None)
        scheduler.run()  # type: ignore[attr-defined]  # 阻塞循环，随进程退出
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("rq Scheduler 异常退出")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("rq_worker")

    settings = get_settings()
    if settings.scheduler_backend != "rq":
        logger.warning(
            "SCHEDULER_BACKEND=%s，无需启动 rq worker（当前使用进程内轮询）。",
            settings.scheduler_backend,
        )
        return

    from app.scheduled_tasks import rq_dispatch

    # 阻塞档连接：rq worker 靠 BLMOVE 长阻塞等任务，不能带 1.5s 读超时，
    # 否则 redis 抛 TimeoutError、rq 直接退出（「启动十几秒自动退出」的根因）。
    conn = rq_dispatch._redis_connection("blocking")
    if conn is None:
        logger.error("Redis 未配置或不可用，rq worker 退出。请设置 REDIS_HOST 或改用 SCHEDULER_BACKEND=poll。")
        sys.exit(1)

    _init_runtime()

    try:
        from rq import Queue, Worker
        from rq_scheduler import Scheduler
    except ImportError:  # pragma: no cover
        logger.error("未安装 rq / rq-scheduler，请先安装依赖（pip install rq rq-scheduler）。")
        sys.exit(1)

    # 消费队列集中由 rq_dispatch 给出（定时任务触发 + SOP 边条件离线编译等
    # 通用后台工作）；新增队列时只改 rq_dispatch.consume_queue_names，不动这里。
    queue_names = rq_dispatch.consume_queue_names()
    queues = [Queue(name, connection=conn) for name in queue_names]
    scheduler = Scheduler(queue_name=settings.scheduled_task_queue, connection=conn)

    threading.Thread(target=_start_scheduler, args=(scheduler,), name="rq-scheduler", daemon=True).start()
    logger.info(
        "rq worker 已启动：queues=%s redis=%s:%s",
        ",".join(queue_names),
        settings.redis_host,
        settings.redis_port,
    )

    # 多队列按声明顺序取：定时任务队列优先，编译队列次之。单 worker 同时只跑
    # 一个 job，编译（秒级到十几秒）会短暂占用消费能力——需要隔离就多起一个
    # 进程并只监听 skill_compile 队列。
    worker = Worker(queues, connection=conn)
    worker.work()  # 阻塞；SIGTERM / SIGINT 触发优雅退出
    logger.info("rq worker 已停止。")


if __name__ == "__main__":
    main()
