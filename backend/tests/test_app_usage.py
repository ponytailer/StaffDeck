"""`app_usage.bump_app_usage` 的计数口径回归测试。

这里测的是「本地清单类 Agent 的真实使用计数」：每次成功执行 +1、并发不丢数、
失败不抛异常把主业务带崩。用 in-memory SQLite + StaticPool，通过参数注入
session factory，不碰真实库。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.app_usage import bump_app_usage
from app.db.models import AppUsageCounter


def _test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _read(engine, key: str) -> int | None:
    with Session(engine) as db:
        return db.exec(
            select(AppUsageCounter.usage_count).where(AppUsageCounter.app_key == key)
        ).first()


def test_bump_creates_row_then_increments():
    engine = _test_engine()
    assert _read(engine, "ocr-assistant") is None

    assert bump_app_usage("ocr-assistant", session_factory=engine) == 1
    assert bump_app_usage("ocr-assistant", session_factory=engine) == 2
    assert bump_app_usage("ocr-assistant", session_factory=engine) == 3
    assert _read(engine, "ocr-assistant") == 3


def test_bump_keeps_keys_independent():
    engine = _test_engine()
    bump_app_usage("ocr-assistant", session_factory=engine)
    bump_app_usage("ocr-assistant", session_factory=engine)
    bump_app_usage("decision-assistant", session_factory=engine)

    assert _read(engine, "ocr-assistant") == 2
    assert _read(engine, "decision-assistant") == 1


def test_bump_accumulates_on_existing_nonzero_row():
    """预置值（如运营手动设的初始次数）要能继续往上加，而不是从 1 重来。"""
    engine = _test_engine()
    with Session(engine) as db:
        db.add(AppUsageCounter(app_key="decision-assistant", usage_count=18))
        db.commit()

    assert bump_app_usage("decision-assistant", session_factory=engine) == 19
    assert _read(engine, "decision-assistant") == 19


def test_bump_ignores_empty_key():
    engine = _test_engine()
    assert bump_app_usage("", session_factory=engine) is None