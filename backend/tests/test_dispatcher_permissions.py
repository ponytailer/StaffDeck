"""调度入口的权限边界。

口径与普通员工一致（复用 agents.py 的 ``_ensure_can_manage_agent``）：
创建者与管理员可改，其他人一律 403。额外的两条是调度入口专有的：
1. 成员必须对当前用户**可见**（否则等于绕开可见性去调用看不见的员工）；
2. 调度入口不能被嵌套/循环引用（禁环由 validate_member_inputs 兜）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    OTHER_ID,
    OWNER_ID,
    TENANT,
    bind_model,
    make_agent,
    make_dispatcher,
    make_model_config,
    make_user,
    member_input,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.api import dispatchers as dispatchers_api  # noqa: E402
from app.db import get_session  # noqa: E402
from app.db.models import AgentProfile, DispatcherMember  # noqa: E402
from app.dispatch.service import replace_members  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402

BASE = "/api/enterprise/dispatchers"


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    session.commit()
    yield session
    session.close()


def _client(db, user_id: str):
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(
        type(_user_holder["user"]), user_id
    )
    return TestClient(app)


# 依赖覆盖需要拿到「同类型」的 user 实例；这里用一个可变容器兜一下。
_user_holder: dict[str, object] = {}


def _install(db, user_id: str) -> TestClient:
    user = db.get(__import__("app.db.models", fromlist=["User"]).User, user_id)
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _create(client, members=None, **overrides):
    payload = {
        "tenant_id": TENANT,
        "name": "前台调度",
        "model_config_id": "model_dispatch",
        "members": [
            m.model_dump()
            for m in (members or [member_input(MEMBER_A), member_input(MEMBER_B)])
        ],
    }
    payload.update(overrides)
    return client.post(BASE, json=payload)


def _seed_dispatcher(db) -> None:
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    bind_model(db, DISPATCHER_ID, "model_dispatch")
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()


# ---------------------------------------------------------------- 管理权


def test_owner_can_update_and_delete(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OWNER_ID)
    updated = client.put(
        f"{BASE}/{DISPATCHER_ID}",
        json={"tenant_id": TENANT, "description": "改过的描述"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "改过的描述"

    deleted = client.delete(f"{BASE}/{DISPATCHER_ID}", params={"tenant_id": TENANT})
    assert deleted.status_code == 200, deleted.text


def test_admin_can_update_others_dispatcher(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, "user_admin")
    updated = client.put(
        f"{BASE}/{DISPATCHER_ID}",
        json={"tenant_id": TENANT, "description": "管理员改的"},
    )
    assert updated.status_code == 200, updated.text


def test_other_user_cannot_update_or_delete(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OTHER_ID)

    updated = client.put(
        f"{BASE}/{DISPATCHER_ID}",
        json={"tenant_id": TENANT, "description": "我不该能改"},
    )
    assert updated.status_code == 403, updated.text

    deleted = client.delete(f"{BASE}/{DISPATCHER_ID}", params={"tenant_id": TENANT})
    assert deleted.status_code == 403, deleted.text
    assert db.get(AgentProfile, DISPATCHER_ID) is not None


def test_other_user_cannot_read_unpublished_dispatcher(db) -> None:
    """可见性与普通员工同口径：别人的未公开入口既进不了列表，也读不到详情。"""
    _seed_dispatcher(db)
    client = _install(db, OTHER_ID)
    detail = client.get(f"{BASE}/{DISPATCHER_ID}", params={"tenant_id": TENANT})
    assert detail.status_code == 403
    listed = client.get(BASE, params={"tenant_id": TENANT})
    assert listed.status_code == 200
    assert listed.json() == []


def test_published_dispatcher_is_readable_by_others(db) -> None:
    """公开到广场的入口，同租户同事可见（前端才能选它开聊）。"""
    _seed_dispatcher(db)
    row = db.get(AgentProfile, DISPATCHER_ID)
    metadata = dict(row.metadata_json or {})
    metadata["published_to_gallery"] = True
    row.metadata_json = metadata
    db.add(row)
    db.commit()
    client = _install(db, OTHER_ID)
    detail = client.get(f"{BASE}/{DISPATCHER_ID}", params={"tenant_id": TENANT})
    assert detail.status_code == 200, detail.text
    listed = client.get(BASE, params={"tenant_id": TENANT})
    assert [item["id"] for item in listed.json()] == [DISPATCHER_ID]


def test_cross_tenant_is_forbidden(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OWNER_ID)
    response = client.get(BASE, params={"tenant_id": "tenant_other"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Tenant mismatch"


# ---------------------------------------------------------------- 可见性


def test_cannot_add_invisible_member(db) -> None:
    client = _install(db, OWNER_ID)
    db.add(make_agent("agent_private", name="别人的私有员工", owner_id=OTHER_ID))
    db.commit()
    response = _create(
        client, members=[member_input(MEMBER_A), member_input("agent_private")]
    )
    assert response.status_code == 403, response.text
    assert "看不见的同事" in response.json()["detail"]


def test_cannot_add_hidden_member(db) -> None:
    """``hidden_from_staffdeck`` 的员工对任何人（含管理员）都不可见。"""
    client = _install(db, "user_admin")
    hidden = make_agent("agent_hidden", name="被隐藏的员工", owner_id=OWNER_ID)
    hidden.metadata_json = {"hidden_from_staffdeck": True}
    db.add(hidden)
    db.commit()
    response = _create(
        client, members=[member_input(MEMBER_A), member_input("agent_hidden")]
    )
    assert response.status_code == 403, response.text


def test_update_rejects_invisible_member(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OWNER_ID)
    db.add(make_agent("agent_private", name="别人的私有员工", owner_id=OTHER_ID))
    db.commit()
    response = client.put(
        f"{BASE}/{DISPATCHER_ID}",
        json={
            "tenant_id": TENANT,
            "members": [
                member_input(MEMBER_A).model_dump(),
                member_input("agent_private").model_dump(),
            ],
        },
    )
    assert response.status_code == 403, response.text
    # 原配置未被改坏
    rows = db.exec(
        select(DispatcherMember).where(DispatcherMember.dispatcher_agent_id == DISPATCHER_ID)
    ).all()
    assert {row.member_agent_id for row in rows} == {MEMBER_A, MEMBER_B}


# ---------------------------------------------------------------- 禁环


def test_dispatcher_cannot_be_member_of_another(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OWNER_ID)
    db.add(make_dispatcher("agent_dispatcher_2", name="第二个调度"))
    db.commit()
    response = _create(
        client,
        name="第三个调度",
        members=[member_input(MEMBER_A), member_input("agent_dispatcher_2")],
    )
    assert response.status_code == 400, response.text
    assert "不支持嵌套调度" in response.json()["detail"]


def test_cannot_bind_self_via_update(db) -> None:
    _seed_dispatcher(db)
    client = _install(db, OWNER_ID)
    response = client.put(
        f"{BASE}/{DISPATCHER_ID}",
        json={
            "tenant_id": TENANT,
            "members": [
                member_input(MEMBER_A).model_dump(),
                member_input(DISPATCHER_ID).model_dump(),
            ],
        },
    )
    assert response.status_code == 400, response.text
    assert "不能把自己设为成员" in response.json()["detail"]


def test_get_agent_does_not_leak_dispatcher_kind(db) -> None:
    """``/api/enterprise/agents`` 的读接口要带上 kind，前端才能分组。"""
    from app.api.agents import agent_read

    _seed_dispatcher(db)
    row = db.get(AgentProfile, DISPATCHER_ID)
    read = agent_read(row, [])
    assert read.kind == "dispatcher"
    standard = agent_read(db.get(AgentProfile, MEMBER_A), [])
    assert standard.kind == "standard"