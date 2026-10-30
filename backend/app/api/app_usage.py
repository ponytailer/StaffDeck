"""「本地清单类」Agent 的真实使用计数。

Agent 广场里的定制 Agent（`agent-catalog.json`）是**前端本地清单、不落库**，
没有 `agent_profiles` 行，也就没有 `usage_count`；但首页热门排行榜要统计它们。
这里给这类应用一个全局计数器 `app_usage_counters`（app_key 全局唯一、不按租户分）。

口径 —— **每次成功执行计一次**，也就是「跑出了一次结果」才算：

- OCR 助手：一次 `ocr:run` 请求（不管当时传了几个文件）算一次。
  文件校验失败、上游报错都不计 —— 用户没拿到结果，不该记一次使用。
- 决策助手：一次 `laya/predict` 请求（不管里面几个问题）算一次。

企业端登录态与开放 API（`sd_live_*` 密钥）共用同一份执行体，所以计数点放在执行体
内部而不是路由上：两个入口各写一遍很容易漏掉一边。

写库用 SQL 侧自增（`usage_count = usage_count + 1`）而不是 read-modify-write，
避免并发请求互相覆盖；并且**用独立 Session 自己提交**：计数是纯统计，
不能因为主业务回滚把这次「已经发生过的使用」一起撤掉，也不能让计数的 INSERT
把主事务的 flush 顺序搅乱。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select, update

from app.db.database import engine
from app.db.models import AppUsageCounter, utc_now

logger = logging.getLogger(__name__)


def bump_app_usage(app_key: str, *, session_factory: Any = None) -> int | None:
    """给 `app_key` 计数 +1，返回自增后的值；失败返回 ``None``（不抛异常）。

    计数失败绝不影响主业务：调用方拿到 ``None`` 就该当没事发生。

    ``session_factory`` 默认用全局 `engine`（生产路径）；测试传一个 in-memory
    engine 即可，不必污染真实库。
    """
    if not app_key:
        return None
    factory = session_factory or engine
    try:
        with Session(factory) as db:
            _ensure_counter_row(db, app_key)
            db.exec(
                update(AppUsageCounter)
                .where(AppUsageCounter.app_key == app_key)
                .values(usage_count=AppUsageCounter.usage_count + 1, updated_at=utc_now())
                .execution_options(synchronize_session=False)
            )
            db.commit()
            value = db.exec(
                select(AppUsageCounter.usage_count).where(AppUsageCounter.app_key == app_key)
            ).first()
            return max(0, int(value or 0))
    except SQLAlchemyError:
        logger.warning("app usage counter bump failed: app_key=%s", app_key, exc_info=True)
        return None


def _ensure_counter_row(db: Session, app_key: str) -> None:
    """保证计数行存在。

    并发下两个请求可能同时 INSERT，唯一约束让其中的一个失败；这里吞掉该冲突后
    回滚，随后由调用方的 UPDATE 补上 —— 最坏情况只是白跑一次建行，计数不会丢。
    """
    if db.exec(select(AppUsageCounter.id).where(AppUsageCounter.app_key == app_key)).first():
        return
    db.add(AppUsageCounter(app_key=app_key, usage_count=0))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()