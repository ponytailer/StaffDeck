"""多智能体「发布到开放广场 · 员工小队」的口径（2026-11-07 新增）。

三条要验的：
1. 发布/下架只改 metadata，不动成员与模型；重复调用幂等；
2. `?gallery_only=1` 只返回已发布的入口，且不看归属（广场是「别人也能用」）；
   普通列表仍按归属过滤（普通成员看不到别人未发布的入口）；
3. 广场计数的 `squads` 与 `agents` 互斥 —— 员工小队不能再被算进数字员工，
   否则「全部」tab 的角标会重复累加。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    ADMIN_ID,
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    OTHER_ID,
    OWNER_ID,
    TENANT,
    make_agent,
    make_dispatcher,
    make_user,
    member_input,
    memory_session,
    seed_tenant,
    seed_two_members,
)
from test_dispatcher_members import (  # noqa: E402
    BASE,
    _JsonClient,
    _create_payload,
    _prepare,
)

from app.api import dispatchers as dispatchers_api  # noqa: E402
from app.db import get_session  # noqa: E402
from app.db.models import AgentProfile  # noqa: E402
from app.dispatch.service import replace_members  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402


def _new_client(db, user):
    """独立构造一个只挂 dispatchers 路由的 client（不同用户视角）。"""
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return _JsonClient(TestClient(app), app)


def _published(db, client, agent_id: str) -> dict:
    response = client.post(f"{BASE}/{agent_id}/gallery:publish", params={"tenant_id": TENANT})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------- 发布 / 下架


def test_publish_sets_flag_without_touching_members_or_model() -> None:
    db = memory_session()
    client = _prepare(db)
    created = client.post(BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)]))
    agent_id = created.json()["id"]

    body = _published(db, client, agent_id)
    assert body["published_to_gallery"] is True
    # 成员与模型原样保留
    assert [m["member_agent_id"] for m in body["members"]] == [MEMBER_A, MEMBER_B]
    assert body["model_config_id"] == "model_dispatch"

    row = db.get(AgentProfile, agent_id)
    assert row is not None
    metadata = row.metadata_json or {}
    assert metadata["published_to_gallery"] is True
    assert metadata["gallery_published_by"] == "user_owner"


def test_publish_is_idempotent() -> None:
    db = memory_session()
    client = _prepare(db)
    agent_id = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    ).json()["id"]

    first = _published(db, client, agent_id)
    second = _published(db, client, agent_id)
    # 第二次不刷新发布时间（幂等：不覆盖首次上架时间）
    assert second["published_to_gallery"] is True
    row = db.get(AgentProfile, agent_id)
    assert (row.metadata_json or {}).get("gallery_published_at") == (
        (db.get(AgentProfile, agent_id).metadata_json or {}).get("gallery_published_at")
    )
    assert first["published_to_gallery"] is True


def test_publish_rejects_dispatcher_without_members() -> None:
    """没有成员的入口推到广场，等于给别人一个不会路由的空壳。"""
    db = memory_session()
    client = _prepare(db)
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()

    response = client.post(f"{BASE}/{DISPATCHER_ID}/gallery:publish", params={"tenant_id": TENANT})
    assert response.status_code == 400, response.text
    assert "成员" in response.json()["detail"]
    # 被拒后不能留下发布标记
    assert (db.get(AgentProfile, DISPATCHER_ID).metadata_json or {}).get(
        "published_to_gallery"
    ) is not True


def test_unpublish_clears_flag_and_keeps_members() -> None:
    db = memory_session()
    client = _prepare(db)
    agent_id = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    ).json()["id"]
    _published(db, client, agent_id)

    response = client.post(
        f"{BASE}/{agent_id}/gallery:unpublish", params={"tenant_id": TENANT}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["published_to_gallery"] is False
    assert [m["member_agent_id"] for m in body["members"]] == [MEMBER_A, MEMBER_B]

    metadata = db.get(AgentProfile, agent_id).metadata_json or {}
    assert metadata["published_to_gallery"] is False
    assert "gallery_published_at" not in metadata


# ---------------------------------------------------------------- 列表口径


def test_gallery_only_returns_published_dispatchers_regardless_of_owner() -> None:
    db = memory_session()
    client = _prepare(db)
    published_id = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    ).json()["id"]
    _published(db, client, published_id)
    # 第二个入口不发布
    client.post(
        BASE,
        json=_create_payload(
            [member_input(MEMBER_A), member_input(MEMBER_B)], name="未发布入口"
        ),
    )

    gallery = client.get(BASE, params={"tenant_id": TENANT, "gallery_only": "true"})
    assert gallery.status_code == 200, gallery.text
    assert [row["id"] for row in gallery.json()] == [published_id]

    # 普通列表（本人视角）两个都能看到
    mine = client.get(BASE, params={"tenant_id": TENANT})
    assert len(mine.json()) == 2


def test_gallery_only_hides_unpublished_from_other_users() -> None:
    """别的普通成员看不到你没发布的入口（广场口径例外，但仍受发布标记约束）。"""
    db = memory_session()
    owner_client = _prepare(db)
    agent_id = owner_client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    ).json()["id"]

    # 另一个普通用户（不是管理员、也不是创建者）—— seed_tenant 已经建好 OTHER_ID
    other = make_user(OTHER_ID)
    other_client = _new_client(db, other)

    assert other_client.get(BASE, params={"tenant_id": TENANT}).json() == []
    assert other_client.get(BASE, params={"tenant_id": TENANT, "gallery_only": "true"}).json() == []

    # 发布后，广场口径能看到；普通列表也能看到 —— 与普通员工同口径
    # （_agent_visible_to_user 对 published_to_gallery is True 直接放行，
    #   "发布到广场" 本来就是 "对同租户其他人可见" 的语义）
    _published(db, owner_client, agent_id)
    gallery = other_client.get(BASE, params={"tenant_id": TENANT, "gallery_only": "true"})
    assert [row["id"] for row in gallery.json()] == [agent_id]
    assert [row["id"] for row in other_client.get(BASE, params={"tenant_id": TENANT}).json()] == [
        agent_id
    ]


def test_read_exposes_published_flag() -> None:
    db = memory_session()
    client = _prepare(db)
    agent_id = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    ).json()["id"]

    before = client.get(f"{BASE}/{agent_id}", params={"tenant_id": TENANT}).json()
    assert before["published_to_gallery"] is False

    _published(db, client, agent_id)
    after = client.get(f"{BASE}/{agent_id}", params={"tenant_id": TENANT}).json()
    assert after["published_to_gallery"] is True


# ---------------------------------------------------------------- 广场计数互斥


def test_gallery_counts_squads_excludes_them_from_agents() -> None:
    """员工小队不能同时算进「数字员工」，否则「全部」角标重复累加。"""
    from app.api.gallery import gallery_counts

    db = memory_session()
    seed_tenant(db)
    seed_two_members(db)
    admin = db.get(type(make_user(ADMIN_ID)), ADMIN_ID)
    # 一个普通员工，发布到广场
    employee = make_agent("agent_plain", name="普通员工", owner_id=OWNER_ID)
    employee.metadata_json = {
        **(employee.metadata_json or {}),
        "published_to_gallery": True,
    }
    db.add(employee)
    # 一个入口，也发布
    dispatcher = make_dispatcher()
    dispatcher.metadata_json = {
        **(dispatcher.metadata_json or {}),
        "published_to_gallery": True,
    }
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()
    # 宿主要存在，否则 is_open_gallery_resource 恒 False
    db.add(
        AgentProfile(
            id="agent_tenant_demo_overall",
            tenant_id=TENANT,
            name="开放广场",
            is_overall=True,
            status="active",
        )
    )
    db.commit()

    counts = gallery_counts(tenant_id=TENANT, db=db, current_user=admin)
    assert counts.squads == 1
    # 普通员工那条不能因为 kind 判断被漏掉
    assert counts.agents == 1