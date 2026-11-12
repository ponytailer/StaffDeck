"""users.updated_at 当「最近活跃」的节流打点。

用户反馈：账号管理页的「最近更新」从未变过（只有资料编辑才会刷新，
平时根本没人改）→ 口径改为「有页面/API 访问就刷新，30 分钟内不重复更新」。
"""

from __future__ import annotations

from datetime import timedelta
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import Tenant, User, utc_now
from app.security.auth import (
    USER_ACTIVITY_TOUCH_INTERVAL_SECONDS,
    _touch_user_activity,
    invalidate_user_cache,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _make_user(db: Session, *, updated_at) -> User:
    db.add(Tenant(id="tenant_demo", name="demo"))
    user = User(
        id="user_demo",
        tenant_id="tenant_demo",
        username="demo",
        password_hash="x",
        updated_at=updated_at,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_touch_refreshes_stale_updated_at(monkeypatch) -> None:
    """超过 30 分钟没活动 → 访问把 updated_at 刷到当前时间。"""
    monkeypatch.setenv("REDIS_HOST", "")
    with _test_session() as db:
        stale = utc_now() - timedelta(seconds=USER_ACTIVITY_TOUCH_INTERVAL_SECONDS + 60)
        user = _make_user(db, updated_at=stale)
        invalidate_user_cache(user.id)

        # 认证缓存对象（detached）上的旧值——与 get_current_user 拿到的一致
        cached = User(
            id=user.id, tenant_id=user.tenant_id, username=user.username,
            password_hash="", updated_at=stale,
        )
        _touch_user_activity(db, cached)

        db.expire_all()
        refreshed = db.get(User, user.id)
        assert refreshed.updated_at is not None
        assert (utc_now() - refreshed.updated_at).total_seconds() < 5


def test_touch_within_throttle_window_is_noop(monkeypatch) -> None:
    """30 分钟窗口内 → 不写库（DB 里的 updated_at 保持原值）。"""
    monkeypatch.setenv("REDIS_HOST", "")
    with _test_session() as db:
        recent = utc_now() - timedelta(seconds=60)
        user = _make_user(db, updated_at=recent)
        invalidate_user_cache(user.id)

        cached = User(
            id=user.id, tenant_id=user.tenant_id, username=user.username,
            password_hash="", updated_at=recent,
        )
        _touch_user_activity(db, cached)

        db.expire_all()
        assert db.get(User, user.id).updated_at == recent


def test_touch_within_window_skips_even_if_cache_stale(monkeypatch) -> None:
    """缓存对象谎报旧值（如缓存重建丢字段）时，DB 二次检查仍然拦截。"""
    monkeypatch.setenv("REDIS_HOST", "")
    with _test_session() as db:
        recent = utc_now() - timedelta(seconds=120)
        user = _make_user(db, updated_at=recent)
        invalidate_user_cache(user.id)

        cached = User(
            id=user.id, tenant_id=user.tenant_id, username=user.username,
            password_hash="", updated_at=None,  # 缓存侧以为从没活跃过
        )
        _touch_user_activity(db, cached)

        db.expire_all()
        assert db.get(User, user.id).updated_at == recent


def test_touch_when_updated_at_missing(monkeypatch) -> None:
    """updated_at 为空（历史数据）→ 视为需要刷新。"""
    monkeypatch.setenv("REDIS_HOST", "")
    with _test_session() as db:
        user = _make_user(db, updated_at=None)
        invalidate_user_cache(user.id)

        cached = User(
            id=user.id, tenant_id=user.tenant_id, username=user.username,
            password_hash="", updated_at=None,
        )
        _touch_user_activity(db, cached)

        db.expire_all()
        refreshed = db.get(User, user.id)
        assert refreshed.updated_at is not None
        assert (utc_now() - refreshed.updated_at).total_seconds() < 5


def test_touch_missing_user_is_safe(monkeypatch) -> None:
    """用户已被删除（缓存还有残留）→ 打点静默跳过，不抛错。"""
    monkeypatch.setenv("REDIS_HOST", "")
    with _test_session() as db:
        stale = utc_now() - timedelta(seconds=USER_ACTIVITY_TOUCH_INTERVAL_SECONDS * 2)
        user = _make_user(db, updated_at=stale)
        invalidate_user_cache(user.id)

        cached = User(
            id=user.id, tenant_id=user.tenant_id, username=user.username,
            password_hash="", updated_at=stale,
        )
        db.delete(db.get(User, user.id))
        db.commit()

        # 不应抛出任何异常
        _touch_user_activity(db, cached)
