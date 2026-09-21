from __future__ import annotations

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm.exc import ObjectDeletedError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.bulk_delete import bulk_delete_matching, bulk_delete_where, expunge_matching
from app.db.models import HarnessAgentLoopRecord


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _loop(tenant_id: str, session_id: str, suffix: str) -> HarnessAgentLoopRecord:
    return HarnessAgentLoopRecord(
        id=f"hloop_{suffix}",
        tenant_id=tenant_id,
        session_id=session_id,
        loop_key=f"general:{suffix}",
        kind="general",
        status="active",
    )


def test_bulk_delete_matching_is_scoped_and_rowcount_is_exact() -> None:
    """只删命中 tenant_id + session_id 的行，其它租户/会话的行必须原样保留。"""
    engine = _engine()
    with Session(engine) as db:
        db.add_all(
            [
                _loop("tenant_a", "session_1", "a1"),
                _loop("tenant_a", "session_2", "a2"),
                _loop("tenant_b", "session_1", "b1"),
            ]
        )
        db.commit()

        deleted = bulk_delete_matching(
            db, HarnessAgentLoopRecord, tenant_id="tenant_a", session_id="session_1"
        )
        db.commit()

        assert deleted == 1
        assert db.get(HarnessAgentLoopRecord, "hloop_a1") is None
        assert db.get(HarnessAgentLoopRecord, "hloop_a2") is not None
        assert db.get(HarnessAgentLoopRecord, "hloop_b1") is not None


def test_bare_bulk_delete_breaks_already_loaded_objects() -> None:
    """反证：不经 expunge 的裸 Core DELETE 会把已加载对象搞坏。

    这条用例存在是为了说明 expunge 为什么必要 —— 单独跑 bulk_delete 前如果会话里
    已有该行对象，commit 会把它 expire，之后再读任一列都会去刷新已不存在的行，
    抛 ObjectDeletedError。删掉 expunge 时，下面这条断言就会失败。
    """
    engine = _engine()
    with Session(engine) as db:
        db.add(_loop("tenant_a", "session_1", "a1"))
        db.commit()

        obj = db.get(HarnessAgentLoopRecord, "hloop_a1")
        db.commit()  # 过期化：属性变为「下次访问时重新查库」

        # 裸 DELETE 绕过 ORM，会话里仍认为该对象是 persistent
        db.exec(
            sa_delete(HarnessAgentLoopRecord).where(
                HarnessAgentLoopRecord.tenant_id == "tenant_a",
                HarnessAgentLoopRecord.session_id == "session_1",
            )
        )
        db.commit()

        with pytest.raises(ObjectDeletedError):
            _ = obj.session_id


def test_bulk_delete_matching_keeps_already_loaded_objects_usable() -> None:
    """expunge 存在的意义：批量删除后，调用方手里已加载的对象仍然可用。

    与上一条用例完全同形，只把裸 DELETE 换成 bulk_delete_matching。
    """
    engine = _engine()
    with Session(engine) as db:
        db.add(
            _loop("tenant_a", "session_1", "a1"),
        )
        db.add(
            _loop("tenant_a", "session_2", "a2"),
        )
        db.commit()

        target = db.get(HarnessAgentLoopRecord, "hloop_a1")
        survivor = db.get(HarnessAgentLoopRecord, "hloop_a2")
        db.commit()

        bulk_delete_matching(
            db, HarnessAgentLoopRecord, tenant_id="tenant_a", session_id="session_1"
        )
        db.commit()

        # 已删除对象：属性仍可读（不抛 ObjectDeletedError），db.get 干净地返回 None
        assert target.session_id == "session_1"
        assert target.id == "hloop_a1"
        assert db.get(HarnessAgentLoopRecord, "hloop_a1") is None

        # 未删除对象：一切照旧
        assert survivor.session_id == "session_2"
        assert db.get(HarnessAgentLoopRecord, survivor.id) is survivor


def test_bulk_delete_where_supports_non_equality_criteria() -> None:
    """低层原语要能吃 != / in_ 这类条件（memories 清理用的是 !=）。"""
    engine = _engine()
    with Session(engine) as db:
        db.add_all(
            [
                _loop("tenant_a", "session_1", "a1"),
                _loop("tenant_a", "session_2", "a2"),
            ]
        )
        db.commit()

        deleted = bulk_delete_where(
            db,
            HarnessAgentLoopRecord,
            HarnessAgentLoopRecord.tenant_id == "tenant_a",
            HarnessAgentLoopRecord.session_id != "session_1",
        )
        db.commit()

        assert deleted == 1
        assert db.get(HarnessAgentLoopRecord, "hloop_a1") is not None
        assert db.get(HarnessAgentLoopRecord, "hloop_a2") is None


def test_expunge_matching_only_detaches_matching_objects() -> None:
    engine = _engine()
    with Session(engine) as db:
        db.add_all(
            [
                _loop("tenant_a", "session_1", "a1"),
                _loop("tenant_b", "session_1", "b1"),
            ]
        )
        db.commit()
        loaded = list(db.exec(select(HarnessAgentLoopRecord)).all())

        assert expunge_matching(db, HarnessAgentLoopRecord, tenant_id="tenant_a") == 1
        remaining = {obj.id for obj in db.identity_map.values()}
        assert "hloop_b1" in remaining
        assert "hloop_a1" not in remaining
        assert len(loaded) == 2
