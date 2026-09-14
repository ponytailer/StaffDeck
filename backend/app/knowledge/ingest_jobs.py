"""知识库文档入库的异步调度与执行入口。
入库是「上传接口立即返回、后台慢慢跑」的长任务：
解析 → 规范化 → 章节树 → 主题规划（LLM）→ 知识发现（LLM）→ 切片落库，
实测 23 页 PDF 约 3~10 分钟，随文档体量与上游模型网关抖动浮动。
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.db import engine

logger = logging.getLogger(__name__)

JOB_NAME = "knowledge_ingest"

# rq 用点分路径跨进程解析函数（``rq_dispatch._resolve_job_func`` 会把冒号形式
# 归一化，但这里直接写对，避免入队前的多余解析失败）。
JOB_FUNC_PATH = "app.knowledge.ingest_jobs.run_ingest_job"


def run_ingest_job(job_id: str) -> None:
    """执行一次入库任务（rq worker 与进程内队列共用的入口）。

    只接收 ``job_id``：任务的全部输入（文件内容 base64、标题、元数据）在
    ``create_ingest_job`` 时已写入 ``knowledge_ingest_jobs.metadata_json``，
    worker 回读 PG 即可。入队参数因此保持 JSON 可序列化，既不把几 MB 的
    base64 塞进 Redis 队列，也天然跨进程安全。

    ``KnowledgeService.run_ingest_job`` 本身会开一个**自己的** Session：
    不能沿用请求期 Session（上传接口返回后它就被关了），所以这里的 Session
    只是给构造函数一个合法入参，真正的读写发生在内层。
    """

    from app.knowledge.service import KnowledgeService

    with Session(engine) as db:
        KnowledgeService(db).run_ingest_job(job_id)


def schedule_knowledge_ingest(
    job_id: str,
    *,
    tenant_id: str | None = None,
    filename: str | None = None,
) -> bool:
    """调度一次入库任务，返回是否成功排入某个通道。

    调用方（文档上传 / 团队黑板沉淀 / OKF 导入）**不等待结果**，只负责把
    ``job_id`` 排进队列并立即把 job 状态返回给前端。rq 不可用时自动降级到
    进程内异步队列，因此除非两条通道都不可用，本函数都返回 ``True``。
    """

    from app.config import get_settings

    settings = get_settings()

    try:
        from app.scheduled_tasks import rq_dispatch

        rq_job_id = rq_dispatch.enqueue_job(
            settings.knowledge_ingest_queue,
            JOB_FUNC_PATH,
            job_id,
            timeout_seconds=settings.knowledge_ingest_job_timeout_seconds,
        )
        if rq_job_id:
            logger.debug("入库任务已入 rq 队列 job=%s rq_job=%s", job_id, rq_job_id)
            return True
    except Exception:  # noqa: BLE001 - rq 装配异常不应影响上传接口
        logger.warning("rq 入库任务入队失败，降级进程内队列 job=%s", job_id, exc_info=True)

    try:
        from app.async_jobs import enqueue_async_job

        enqueue_async_job(
            JOB_NAME,
            run_ingest_job,
            job_id,
            metadata={"tenant_id": tenant_id, "filename": filename},
        )
        return True
    except Exception:  # noqa: BLE001 - 后台调度失败不该让上传接口 500
        logger.warning("入库任务调度失败 job=%s", job_id, exc_info=True)
        return False
