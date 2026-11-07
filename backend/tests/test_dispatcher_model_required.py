"""「必须配置模型」与「调度入口不许绑资源/标免费」的闸门。

这些约束合起来保证一件事：**调度入口永远不走免费额度链路**。
只要有一道闸漏了，被标记免费用户就能靠它刷免费模型的日配额。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    OWNER_ID,
    TENANT,
    bind_model,
    make_dispatcher,
    make_model_config,
    make_user,
    member_input,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.api import agents as agents_api  # noqa: E402
from app.api import dispatchers as dispatchers_api  # noqa: E402
from app.db import get_session  # noqa: E402
from app.db.models import AgentProfile, ModelConfig, User  # noqa: E402
from app.dispatch.service import resolve_dispatch_member_model  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402

BASE = "/api/enterprise/dispatchers"


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID, is_default=False))
    session.commit()
    yield session
    session.close()


def _client(db, user):
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.include_router(agents_api.enterprise_router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _create(db, client, **overrides):
    payload = {
        "tenant_id": TENANT,
        "name": "前台调度",
        "model_config_id": "model_dispatch",
        "members": [member_input(MEMBER_A).model_dump(), member_input(MEMBER_B).model_dump()],
    }
    payload.update(overrides)
    return client.post(BASE, json=payload)


# ---------------------------------------------------------------- 模型必填


def test_create_without_model_is_rejected(db) -> None:
    client = _client(db, make_user(OWNER_ID))
    response = _create(db, client, model_config_id=None)
    assert response.status_code == 400, response.text
    assert "must configure a model" in response.json()["detail"]
    assert db.exec(select(AgentProfile).where(AgentProfile.name == "前台调度")).first() is None


def test_create_with_unknown_model_is_rejected(db) -> None:
    client = _client(db, make_user(OWNER_ID))
    response = _create(db, client, model_config_id="model_nope")
    assert response.status_code == 400
    assert response.json()["detail"] == "Model config not found"


def test_create_with_disabled_model_is_rejected(db) -> None:
    row = db.get(ModelConfig, "model_dispatch")
    row.enabled = False
    db.add(row)
    db.commit()
    client = _client(db, make_user(OWNER_ID))
    response = _create(db, client)
    assert response.status_code == 400
    assert response.json()["detail"] == "Model config is disabled"


def test_create_with_cross_tenant_model_is_rejected(db) -> None:
    db.add(make_model_config("model_other", user_id=None, tenant_id="tenant_other"))
    db.commit()
    client = _client(db, make_user(OWNER_ID))
    response = _create(db, client, model_config_id="model_other")
    assert response.status_code == 400
    assert response.json()["detail"] == "Model config not found"


def test_update_cannot_strip_model(db) -> None:
    client = _client(db, make_user(OWNER_ID))
    created = _create(db, client)
    assert created.status_code == 200, created.text
    # 显式传空串 = 想摘掉模型 → 拒绝
    response = client.put(
        f"{BASE}/{created.json()['id']}",
        json={"tenant_id": TENANT, "model_config_id": ""},
    )
    assert response.status_code == 400
    assert "must configure a model" in response.json()["detail"]
    # 原绑定还在
    assert db.get(AgentProfile, created.json()["id"]) is not None
    assert dispatchers_api.dispatcher_model_config_id(
        db, TENANT, created.json()["id"]
    ) == "model_dispatch"


def test_update_can_switch_model(db) -> None:
    db.add(make_model_config("model_new", user_id=OWNER_ID))
    db.commit()
    client = _client(db, make_user(OWNER_ID))
    created = _create(db, client)
    agent_id = created.json()["id"]
    response = client.put(
        f"{BASE}/{agent_id}",
        json={"tenant_id": TENANT, "model_config_id": "model_new"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model_config_id"] == "model_new"
    assert dispatchers_api.dispatcher_model_config_id(db, TENANT, agent_id) == "model_new"


# ---------------------------------------------------------------- 资源闸门


def test_dispatcher_cannot_bind_resources(db) -> None:
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    admin = make_user("user_admin", role="admin")

    with pytest.raises(HTTPException) as excinfo:
        agents_api.update_agent_resources(
            DISPATCHER_ID,
            agents_api.AgentResourcesUpdateRequest(
                tenant_id=TENANT,
                resources=[
                    {"resource_type": "knowledge_base", "resource_id": "kb_1"}
                ],
            ),
            db=db,
            current_user=admin,
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == agents_api.DISPATCHER_RESOURCE_ERROR


def test_dispatcher_cannot_be_marked_free(db) -> None:
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    admin = make_user("user_admin", role="admin")

    with pytest.raises(HTTPException) as excinfo:
        agents_api.set_agent_free_model(
            DISPATCHER_ID,
            agents_api.AgentFreeModelRequest(enabled=True),
            tenant_id=TENANT,
            db=db,
            current_user=admin,
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == agents_api.DISPATCHER_FREE_ERROR
    db.refresh(dispatcher)
    assert dispatcher.free_model_enabled is False


def test_standard_agent_still_can_be_marked_free(db) -> None:
    """闸门只拦 dispatcher，别把普通员工的既有能力一起掐掉。"""
    db.add(make_model_config("model_global_free", user_id=None, is_global_free=True))
    db.commit()
    admin = make_user("user_admin", role="admin")
    row = agents_api.set_agent_free_model(
        MEMBER_A,
        agents_api.AgentFreeModelRequest(enabled=True),
        tenant_id=TENANT,
        db=db,
        current_user=admin,
    )
    assert row.free_model_enabled is True


# ---------------------------------------------------------------- 不吃免费链路


def test_member_model_resolution_ignores_global_free(db, monkeypatch) -> None:
    """即使租户有全局免费模型，成员没配模型时也只能拿到调度入口的模型。"""
    db.add(make_model_config("model_global_free", user_id=None, is_global_free=True))
    db.commit()

    def _forbidden(*args, **kwargs):
        raise AssertionError("免费回落不该被触发")

    monkeypatch.setattr(
        "app.agents.branching.resolve_turn_default_model", _forbidden, raising=False
    )

    from app.dispatch.service import resolve_dispatcher_model

    dispatcher = make_dispatcher()
    bind_model(db, dispatcher.id, "model_dispatch")
    db.add(dispatcher)
    db.commit()
    dispatcher_model = resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)

    bare = AgentProfile(id="agent_bare2", tenant_id=TENANT, name="无模型同事")
    db.add(bare)
    db.commit()
    resolved = resolve_dispatch_member_model(db, TENANT, bare, dispatcher_model, OWNER_ID)
    assert resolved.id == "model_dispatch"


def test_member_binding_to_disabled_model_falls_back(db) -> None:
    """成员绑的模型后来被停用 → 回落到调度入口的模型，而不是整轮失败。"""
    db.add(make_model_config("model_stale", user_id=OWNER_ID, enabled=False))
    db.commit()
    db.add(
        AgentProfile(
            id="agent_stale", tenant_id=TENANT, name="绑了停用模型的同事", owner_id=OWNER_ID
        )
    )
    db.commit()
    bind_model(db, "agent_stale", "model_stale")
    db.commit()

    from app.dispatch.service import resolve_dispatcher_model

    dispatcher = make_dispatcher()
    bind_model(db, dispatcher.id, "model_dispatch")
    db.add(dispatcher)
    db.commit()
    dispatcher_model = resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)

    resolved = resolve_dispatch_member_model(
        db, TENANT, db.get(AgentProfile, "agent_stale"), dispatcher_model, OWNER_ID
    )
    assert resolved.id == "model_dispatch"


def test_own_model_takes_priority_over_dispatcher(db) -> None:
    from app.dispatch.service import resolve_dispatcher_model

    dispatcher = make_dispatcher()
    bind_model(db, dispatcher.id, "model_dispatch")
    db.add(dispatcher)
    db.commit()
    dispatcher_model = resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)
    resolved = resolve_dispatch_member_model(
        db, TENANT, db.get(AgentProfile, MEMBER_A), dispatcher_model, OWNER_ID
    )
    assert resolved.id == "model_hr"