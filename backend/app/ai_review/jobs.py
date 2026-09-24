"""AI Review 任务的异步调度与执行入口（与 knowledge.ingest_jobs 同一套模板）。

任务是分钟级的（克隆 + ocr 子代理评审），必须离开 web 进程：排队走 rq
队列 ``ai_review``；Redis 不可用 / backend 非 rq 时降级进程内异步队列，
两条都挂则返回 False，任务停留在 queued 供用户手动重试或删除。
"""

from __future__ import annotations

import logging

from sqlmodel import Session

from app.db import engine

logger = logging.getLogger(__name__)

JOB_NAME = "ai_review"

# rq 用点分路径跨进程解析函数（bound method 无法持久化）。
JOB_FUNC_PATH = "app.ai_review.jobs.run_review_job"


def run_review_job(task_id: str) -> None:
    """执行一次评审任务（rq worker 与进程内队列共用的入口）。

    只接收 ``task_id``：全部输入（MR 快照、附加要求）在建任务时已写入
    ``ai_review_tasks``，worker 回读 PG 即可。评审要跑几分钟，这里自己开
    Session、自管提交——不要沿用请求期 Session。
    """
    from app.db.models import AiReviewTask
    from sqlmodel import select

    with Session(engine) as db:
        task = db.exec(select(AiReviewTask).where(AiReviewTask.id == task_id)).first()
        if task is None:
            logger.warning("AI Review 任务不存在，跳过 task=%s", task_id)
            return
        if task.status in ("succeeded", "failed"):
            return

        from app.ai_review.runner import ReviewRunError, run_review
        from app.db.models import AiReviewCredential, AiReviewRuleFile, AiReviewWorkspace
        from sqlmodel import select as _select

        started_at = _utc_now()
        task.status = "running"
        task.started_at = started_at
        task.error = ""
        db.add(task)
        db.commit()

        workspace = db.exec(
            _select(AiReviewWorkspace).where(AiReviewWorkspace.id == task.workspace_id)
        ).first()
        credential = db.exec(
            _select(AiReviewCredential).where(
                AiReviewCredential.tenant_id == task.tenant_id,
                AiReviewCredential.platform == workspace.platform,
            )
        ).first() if workspace else None
        # 租户级自定义规则文件（ocr rule.json，执行时 --rule 注入）；执行瞬间读取，
        # 之后改规则不影响已入队的这次评审（快照语义与任务行一致）
        rule_file_row = db.exec(
            _select(AiReviewRuleFile).where(AiReviewRuleFile.tenant_id == task.tenant_id)
        ).first()

        try:
            if workspace is None:
                raise ReviewRunError("任务所属的 workspace 已被删除")
            if credential is None or not credential.token:
                raise ReviewRunError(
                    f"平台「{workspace.platform}」还没有配置 access token，"
                    "请到「平台设置」里填写后再提交评审任务。"
                )
            result = run_review(
                platform=workspace.platform,
                repo_url=workspace.repo_url,
                credential_token=credential.token,
                source_branch=task.source_branch,
                target_branch=task.target_branch,
                mr_title=task.mr_title,
                mr_description=task.mr_description,
                requirements=task.requirements,
                rule_file_content=(rule_file_row.content or None) if rule_file_row else None,
            )
        except ReviewRunError as exc:
            task.status = "failed"
            task.error = str(exc)
            task.finished_at = _utc_now()
            db.add(task)
            db.commit()
            logger.info("AI Review 任务失败 task=%s reason=%s", task_id, str(exc)[:200])
            return
        except Exception:  # noqa: BLE001 - 未预期异常同样要落到任务行，别只留日志
            logger.exception("AI Review 任务异常 task=%s", task_id)
            task.status = "failed"
            task.error = "评审执行出现未知错误，请重试或联系管理员查看 worker 日志。"
            task.finished_at = _utc_now()
            db.add(task)
            db.commit()
            return

        task.result_json = result.get("comments") or []
        task.summary_json = {
            "ocr_status": result.get("ocr_status", ""),
            "model": result.get("model", ""),
            "session_id": result.get("session_id", ""),
            **(result.get("summary") or {}),
        }
        task.status = "succeeded"
        task.finished_at = _utc_now()
        db.add(task)
        db.commit()


def schedule_ai_review(task_id: str, *, tenant_id: str | None = None) -> bool:
    """调度一次评审任务，返回是否成功排入某个通道（不等待结果）。"""
    from app.config import get_settings

    settings = get_settings()

    try:
        from app.scheduled_tasks import rq_dispatch

        rq_job_id = rq_dispatch.enqueue_job(
            settings.ai_review_queue,
            JOB_FUNC_PATH,
            task_id,
            timeout_seconds=settings.ai_review_job_timeout_seconds,
        )
        if rq_job_id:
            logger.debug("AI Review 任务已入 rq 队列 task=%s rq_job=%s", task_id, rq_job_id)
            return True
    except Exception:  # noqa: BLE001 - rq 装配异常不应影响创建接口
        logger.warning("rq AI Review 入队失败，降级进程内队列 task=%s", task_id, exc_info=True)

    try:
        from app.ai_review.jobs import run_review_job
        from app.async_jobs import enqueue_async_job

        enqueue_async_job(
            JOB_NAME,
            run_review_job,
            task_id,
            metadata={"tenant_id": tenant_id},
        )
        return True
    except Exception:  # noqa: BLE001 - 后台调度失败不该让创建接口 500
        logger.warning("AI Review 任务调度失败 task=%s", task_id, exc_info=True)
        return False


def _utc_now():
    from app.db.models import utc_now

    return utc_now()
