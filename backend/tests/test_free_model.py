"""全局免费模型:标记接口、配额、对话回合解析。

口径:每用户每员工每天 FREE_MODEL_DAILY_LIMIT 问;只在用户没有自己的模型时自动生效。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.free_model import (
    FREE_MODEL_DAILY_LIMIT,
    clear_global_free_flag,
    consume_free_quota,
    free_model_turn_allowed,
    get_global_free_model,
    remaining_free_quota,
    resolve_turn_default_model,
)
from app.api.agents import list_agents, set_agent_free_model
from app.api.model_configs import set_global_free_model
from app.db.models import AgentProfile, ModelConfig, Tenant, User

GLOBAL_FREE_FLAG_REQUEST_BODY = {"enabled": True}


class _FlagRequest:
    """直连路由函数时替代 pydantic 请求体(FastAPI 的转换在直调里不生效)。"""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> tuple[User, User]:
    db.add(Tenant(id="tenant_demo", name="演示租户"))
    admin = User(
        id="user_admin", tenant_id="tenant_demo", username="admin", role="admin", password_hash="x"
    )
    member = User(
        id="user_member", tenant_id="tenant_demo", username="member", role="member", password_hash="x"
    )
    db.add_all([admin, member])
    db.commit()
    return admin, member


def _model(db: Session, owner: User, config_id: str, enabled: bool = True) -> ModelConfig:
    row = ModelConfig(
        id=config_id,
        tenant_id=owner.tenant_id,
        user_id=owner.id,
        name=config_id,
        api_key_encrypted="enc",
        model="test-model",
        enabled=enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _agent(db: Session, agent_id: str, is_overall: bool = False) -> AgentProfile:
    # published_to_gallery:member 的 list_agents 可见性要求 owner 或已发布广场,
    # 裸 agent 对 member 隐藏会让按 id 取行的用例拿不到数据。
    row = AgentProfile(
        id=agent_id,
        tenant_id="tenant_demo",
        name=agent_id,
        is_overall=is_overall,
        metadata_json={"published_to_gallery": True},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _set_global(db: Session, admin: User, config_id: str):
    return set_global_free_model(
        config_id,
        _FlagRequest(True),
        tenant_id="tenant_demo",
        db=db,
        current_user=admin,
    )


def test_global_free_model_is_tenant_single_selection() -> None:
    with _test_session() as db:
        admin, _member = _seed(db)
        first = _model(db, admin, "model_a")
        second = _model(db, admin, "model_b")

        _set_global(db, admin, "model_a")
        assert get_global_free_model(db, "tenant_demo").id == "model_a"

        # 置第二个自动清掉第一个(租户内单选)
        _set_global(db, admin, "model_b")
        assert get_global_free_model(db, "tenant_demo").id == "model_b"
        db.refresh(first)
        assert first.is_global_free is False

        # 取消后无全局模型
        set_global_free_model(
            "model_b", _FlagRequest(False), tenant_id="tenant_demo", db=db, current_user=admin
        )
        assert get_global_free_model(db, "tenant_demo") is None


def test_global_free_model_requires_admin_and_owner() -> None:
    with _test_session() as db:
        admin, member = _seed(db)
        _model(db, admin, "model_a")
        others = _model(db, member, "model_member")

        # 成员不能设置
        with pytest.raises(HTTPException) as denied:
            set_global_free_model(
                "model_a", _FlagRequest(True), tenant_id="tenant_demo", db=db, current_user=member
            )
        assert denied.value.status_code == 403

        # 管理员不能标记别人的模型(费用归属属主)
        with pytest.raises(HTTPException) as not_owner:
            _set_global(db, admin, "model_member")
        assert not_owner.value.status_code == 403

        # 停用的模型不能设为全局
        _model(db, admin, "model_disabled", enabled=False)
        with pytest.raises(HTTPException) as disabled:
            _set_global(db, admin, "model_disabled")
        assert disabled.value.status_code == 400


def test_set_agent_free_model_guardrails() -> None:
    with _test_session() as db:
        admin, member = _seed(db)
        agent = _agent(db, "agent_1")
        overall = _agent(db, "agent_overall", is_overall=True)

        # 没有全局模型时不能启用
        with pytest.raises(HTTPException) as no_model:
            set_agent_free_model(
                "agent_1",
                _FlagRequest(True),
                tenant_id="tenant_demo",
                db=db,
                current_user=admin,
            )
        assert no_model.value.status_code == 400

        _model(db, admin, "model_a")
        _set_global(db, admin, "model_a")

        # 成员不能标记
        with pytest.raises(HTTPException) as denied:
            set_agent_free_model(
                "agent_1",
                _FlagRequest(True),
                tenant_id="tenant_demo",
                db=db,
                current_user=member,
            )
        assert denied.value.status_code == 403

        # overall 宿主不能标记
        with pytest.raises(HTTPException) as overall_err:
            set_agent_free_model(
                "agent_overall",
                _FlagRequest(True),
                tenant_id="tenant_demo",
                db=db,
                current_user=admin,
            )
        assert overall_err.value.status_code == 400

        updated = set_agent_free_model(
            "agent_1",
            _FlagRequest(True),
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )
        assert updated.free_model_enabled is True
        # 剩余问数是「当前用户×员工」口径,标记接口不针对当前用户计算(列表接口才算)
        assert updated.free_model_remaining is None


def test_quota_is_per_agent_per_day() -> None:
    with _test_session() as db:
        _admin, member = _seed(db)
        db.add_all([_agent(db, "agent_1"), _agent(db, "agent_2")])
        db.commit()

        for _ in range(FREE_MODEL_DAILY_LIMIT):
            assert consume_free_quota(db, "tenant_demo", member.id, "agent_1") is True
        assert consume_free_quota(db, "tenant_demo", member.id, "agent_1") is False
        assert remaining_free_quota(db, "tenant_demo", member.id, "agent_1") == 0

        # 员工之间互相隔离:agent_2 额度不受影响
        assert remaining_free_quota(db, "tenant_demo", member.id, "agent_2") == FREE_MODEL_DAILY_LIMIT
        assert consume_free_quota(db, "tenant_demo", member.id, "agent_2") is True


def test_free_turn_allowed_states() -> None:
    with _test_session() as db:
        admin, member = _seed(db)
        agent = _agent(db, "agent_1")

        # 未标记 → 走原链路
        assert free_model_turn_allowed(db, "tenant_demo", member.id, "agent_1") == (None, "agent_not_free")

        _model(db, admin, "model_a")
        _set_global(db, admin, "model_a")
        set_agent_free_model(
            "agent_1",
            _FlagRequest(True),
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )

        assert free_model_turn_allowed(db, "tenant_demo", member.id, "agent_1")[0].id == "model_a"

        # 打满配额 → 超限错误码
        for _ in range(FREE_MODEL_DAILY_LIMIT):
            consume_free_quota(db, "tenant_demo", member.id, "agent_1")
        code = free_model_turn_allowed(db, "tenant_demo", member.id, "agent_1")[1]
        assert code == "free_model_quota_exhausted"


def test_resolve_turn_model_prefers_own_then_free() -> None:
    with _test_session() as db:
        admin, member = _seed(db)
        agent = _agent(db, "agent_1")
        free_row = _model(db, admin, "model_free")
        _set_global(db, admin, "model_free")
        set_agent_free_model(
            "agent_1",
            _FlagRequest(True),
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )

        # 无自己模型 → 免费模型
        resolved = resolve_turn_default_model(db, "tenant_demo", "agent_1", member.id, "default")
        assert resolved is not None and resolved.id == "model_free"

        # 有自己模型 → 自己的优先
        own = ModelConfig(
            id="model_own",
            tenant_id="tenant_demo",
            user_id=member.id,
            name="model_own",
            api_key_encrypted="enc",
            model="own-model",
            enabled=True,
            is_default=True,
        )
        db.add(own)
        db.commit()
        resolved = resolve_turn_default_model(db, "tenant_demo", "agent_1", member.id, "default")
        assert resolved is not None and resolved.id == "model_own"

        # intent_recognition 角色同理:自己的优先,没有就免费
        resolved = resolve_turn_default_model(db, "tenant_demo", "agent_1", member.id, "intent_recognition")
        assert resolved is not None and resolved.id == "model_own"
        db.delete(own)
        db.commit()
        resolved = resolve_turn_default_model(db, "tenant_demo", "agent_1", member.id, "intent_recognition")
        assert resolved is not None and resolved.id == "model_free"

        # 未标记员工 / 无 agent → None(走原回落)
        plain = _agent(db, "agent_plain")
        assert resolve_turn_default_model(db, "tenant_demo", "agent_plain", member.id) is None
        assert resolve_turn_default_model(db, "tenant_demo", None, member.id) is None
        _ = plain


def test_clear_flag_helper_scopes_to_tenant() -> None:
    with _test_session() as db:
        admin, _member = _seed(db)
        _model(db, admin, "model_a")
        _set_global(db, admin, "model_a")
        clear_global_free_flag(db, "tenant_demo", exclude_config_id="model_a")
        assert get_global_free_model(db, "tenant_demo").id == "model_a"
        clear_global_free_flag(db, "tenant_demo")
        assert get_global_free_model(db, "tenant_demo") is None


def test_list_agents_exposes_free_fields() -> None:
    with _test_session() as db:
        admin, member = _seed(db)
        _agent(db, "agent_1")
        _model(db, admin, "model_a")
        _set_global(db, admin, "model_a")
        set_agent_free_model(
            "agent_1",
            _FlagRequest(True),
            tenant_id="tenant_demo",
            db=db,
            current_user=admin,
        )

        rows = {row.id: row for row in list_agents(tenant_id="tenant_demo", db=db, current_user=member)}
        assert rows["agent_1"].free_model_enabled is True
        assert rows["agent_1"].free_model_remaining == FREE_MODEL_DAILY_LIMIT

        consume_free_quota(db, "tenant_demo", member.id, "agent_1")
        rows = {row.id: row for row in list_agents(tenant_id="tenant_demo", db=db, current_user=member)}
        assert rows["agent_1"].free_model_remaining == FREE_MODEL_DAILY_LIMIT - 1
