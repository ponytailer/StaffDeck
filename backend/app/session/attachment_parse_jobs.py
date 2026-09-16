"""聊天 PDF 附件云端解析（MinerU）的异步调度与执行入口。

上传接口对 PDF 附件「立即返回 + 后台解析」：创建 :class:`AttachmentParseJob`
行后交给本模块调度，前端凭 ``parse_job_id`` 轮询进度。解析产物 markdown 写入
附件暂存目录（``parsed.md``），turn 物化时直接作为 extracted_text_path 提供模型，
不再让模型在沙箱里做注定失败的文字层抽取。
"""

from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.db import engine
from app.db.models import AttachmentParseJob, utc_now
from app.session.attachment_store import (
    read_staged_chat_attachment,
    write_staged_parse_result,
)
from app.session.attachments import MAX_EXTRACTED_TEXT_CHARS
from app.session.mineru_client import MinerUError, parse_pdf_bytes
from app.session.session_schema import ChatAttachmentRead

logger = logging.getLogger(__name__)

JOB_NAME = "attachment_parse"
# rq 用点分路径跨进程解析函数（不能传 bound method，rq 只序列化函数名）。
JOB_FUNC_PATH = "app.session.attachment_parse_jobs.run_parse_job"

# turn 拦截口径：status 处于此集合 = 附件还不能随消息发送（细分阶段看 stage）。
RUNNING_STATUSES = {"queued", "parsing"}
_TERMINAL_STATUSES = {"succeeded", "failed"}


def create_parse_job(
    db: Session,
    attachment: ChatAttachmentRead,
    *,
    tenant_id: str,
    user_id: str,
) -> AttachmentParseJob:
    """为一份已暂存的 PDF 附件建解析任务行（不调度，调度交给 ``schedule_attachment_parse``）。"""

    job = AttachmentParseJob(
        tenant_id=tenant_id,
        user_id=user_id,
        attachment_id=attachment.id,
        filename=attachment.filename,
        status="queued",
        stage="queued",
        progress=0.0,
        metadata_json={
            # 重建暂存读取所需的完整附件指纹（read_staged_chat_attachment 全字段校验）
            "filename": attachment.filename,
            "content_type": attachment.content_type,
            "size": attachment.size,
            "sha256": attachment.sha256,
        },
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def schedule_attachment_parse(
    job_id: str,
    *,
    tenant_id: str | None = None,
    filename: str | None = None,
) -> bool:
    """调度一次解析任务，返回是否成功排入某个通道。

    与知识入库同款兜底：rq 不可用降级进程内 AsyncJob，两条都挂返回 False
    （上传接口照常返回，任务留在 queued 由后续重试/人工处理）。
    """

    from app.config import get_settings

    settings = get_settings()

    try:
        from app.scheduled_tasks import rq_dispatch

        rq_job_id = rq_dispatch.enqueue_job(
            settings.attachment_parse_queue,
            JOB_FUNC_PATH,
            job_id,
            timeout_seconds=settings.attachment_parse_job_timeout_seconds,
        )
        if rq_job_id:
            logger.debug("附件解析任务已入 rq 队列 job=%s rq_job=%s", job_id, rq_job_id)
            return True
    except Exception:  # noqa: BLE001 - rq 装配异常不应影响上传接口
        logger.warning("rq 附件解析入队失败，降级进程内队列 job=%s", job_id, exc_info=True)

    try:
        from app.async_jobs import enqueue_async_job

        enqueue_async_job(
            JOB_NAME,
            run_parse_job,
            job_id,
            metadata={"tenant_id": tenant_id, "filename": filename},
        )
        return True
    except Exception:  # noqa: BLE001 - 后台调度失败不该让上传接口 500
        logger.warning("附件解析任务调度失败 job=%s", job_id, exc_info=True)
        return False


def running_job_ids_for_attachments(
    db: Session,
    *,
    tenant_id: str,
    attachment_ids: list[str],
) -> dict[str, AttachmentParseJob]:
    """按附件 id 批量查最近的解析任务（turn 拦截与物化共用）。

    返回 ``{attachment_id: job}``，只含查到的附件。取每个附件最新一条
    （同附件重复上传会建多个任务）。
    """

    if not attachment_ids:
        return {}
    statement = (
        select(AttachmentParseJob)
        .where(
            AttachmentParseJob.tenant_id == tenant_id,
            AttachmentParseJob.attachment_id.in_(attachment_ids),  # type: ignore[attr-defined]
        )
        .order_by(AttachmentParseJob.created_at.desc())
    )
    jobs: dict[str, AttachmentParseJob] = {}
    for job in db.exec(statement):
        jobs.setdefault(job.attachment_id, job)
    return jobs


def run_parse_job(job_id: str) -> None:
    """执行一次附件解析（rq worker 与进程内队列共用的入口）。

    只接收 ``job_id``：附件指纹在上传时已写入 ``metadata_json``，worker 回读
    PG + 暂存目录即可。开**自己的** Session——请求期 Session 在上传接口返回
    后就已关闭。
    """

    with Session(engine) as db:
        job = db.get(AttachmentParseJob, job_id)
        if job is None:
            logger.warning("附件解析任务不存在 job=%s", job_id)
            return
        if job.status in _TERMINAL_STATUSES:
            logger.info("附件解析任务已完成，跳过 job=%s status=%s", job_id, job.status)
            return

        attachment = _staged_attachment(job)
        if attachment is None:
            _fail(db, job, "附件暂存不存在或校验失败，请重新上传")
            return

        job.status = "parsing"
        job.stage = "uploading"
        job.progress = 1.0
        job.started_at = utc_now()
        job.updated_at = utc_now()
        db.add(job)
        db.commit()

        def report(stage: str, progress: float) -> None:
            # 轮询间隔 5s，每次回写一行；远程 PG 单程 ~10ms，量级可接受。
            job.stage = stage
            job.progress = min(99.0, max(0.0, float(progress)))
            job.updated_at = utc_now()
            db.add(job)
            db.commit()

        try:
            payload = read_staged_chat_attachment(
                attachment,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
            )
        except Exception:  # noqa: BLE001 - 任何暂存读取异常都按失败处理
            payload = None
        if payload is None:
            _fail(db, job, "附件暂存不存在或校验失败，请重新上传")
            return

        try:
            markdown = parse_pdf_bytes(payload, job.filename, progress_cb=report)
        except MinerUError as exc:
            _fail(db, job, str(exc))
            return
        except Exception:  # noqa: BLE001 - 未预期异常也要落到任务状态，不能只进日志
            logger.exception("附件解析任务异常 job=%s", job_id)
            _fail(db, job, "云端解析发生未预期错误，请重试")
            return

        truncated = markdown
        if len(truncated) > MAX_EXTRACTED_TEXT_CHARS:
            truncated = (
                truncated[:MAX_EXTRACTED_TEXT_CHARS].rstrip()
                + "\n\n...（内容已截断，全文请分批提问）"
            )
        try:
            write_staged_parse_result(
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                attachment_id=job.attachment_id,
                markdown=truncated,
            )
        except OSError as exc:
            logger.exception("解析产物写入暂存目录失败 job=%s", job_id)
            _fail(db, job, f"解析产物保存失败：{type(exc).__name__}")
            return

        job.status = "succeeded"
        job.stage = "done"
        job.progress = 100.0
        job.error = None
        job.finished_at = utc_now()
        job.updated_at = utc_now()
        job.metadata_json = {
            **job.metadata_json,
            "text_chars": len(truncated),
            "truncated": len(truncated) != len(markdown),
        }
        db.add(job)
        db.commit()
        logger.info(
            "附件解析完成 job=%s attachment=%s chars=%s",
            job_id,
            job.attachment_id,
            len(truncated),
        )


def _staged_attachment(job: AttachmentParseJob) -> ChatAttachmentRead | None:
    """按任务里保存的指纹重建附件对象，供暂存读取做全字段校验。"""

    meta = job.metadata_json if isinstance(job.metadata_json, dict) else {}
    try:
        return ChatAttachmentRead(
            id=job.attachment_id,
            filename=str(meta.get("filename") or job.filename),
            content_type=str(meta.get("content_type") or "application/pdf"),
            size=int(meta.get("size") or 0),
            kind="pdf",
            sha256=str(meta.get("sha256") or "") or None,
        )
    except (TypeError, ValueError):
        return None


def _fail(db: Session, job: AttachmentParseJob, message: str) -> None:
    logger.warning("附件解析失败 job=%s: %s", job.id, message)
    job.status = "failed"
    job.stage = "failed"
    job.error = message[:500]
    job.finished_at = utc_now()
    job.updated_at = utc_now()
    db.add(job)
    db.commit()


__all__ = [
    "JOB_FUNC_PATH",
    "JOB_NAME",
    "RUNNING_STATUSES",
    "create_parse_job",
    "run_parse_job",
    "running_job_ids_for_attachments",
    "schedule_attachment_parse",
]
