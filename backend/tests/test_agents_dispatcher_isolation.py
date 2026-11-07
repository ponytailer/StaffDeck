"""调度入口不能污染「我的数字员工」列表。

这是个真实踩过的回归：``kind="dispatcher"`` 的行本来是 ``AgentProfile`` 里的一类新
记录，``GET /api/enterprise/agents`` 当初只按租户过滤，于是：

- 员工总数从 11 变成 12（多算了一个「员工」，但它不是员工）；
- 员工卡网格的首张卡变成调度入口，卡片上的「资料 / 技能 / SOP」全是 0，
  因为它按设计就不能绑这些资源 —— 看起来像一张坏掉的员工卡。

前端已经把调度入口放进独立分区（``section[aria-label="调度入口"]``），
所以这个接口必须把自己那半边守住。
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    OWNER_ID,
    TENANT,
    make_dispatcher,
    make_user,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.api import agents as agents_api  # noqa: E402
from app.db import get_session  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402


def _client(db, user):
    app = FastAPI()
    app.include_router(agents_api.enterprise_router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _prepare():
    db = memory_session()
    seed_tenant(db)
    seed_two_members(db)
    db.add(make_dispatcher(DISPATCHER_ID))
    db.commit()
    # 管理员视角：不做可见性收敛，能直接看出接口有没有按 kind 过滤
    return db, _client(db, make_user(OWNER_ID))


def test_enterprise_agents_excludes_dispatcher():
    _db, client = _prepare()

    response = client.get("/api/enterprise/agents", params={"tenant_id": TENANT})

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert DISPATCHER_ID not in ids
    # 两个普通员工必须还在 —— 过滤条件别写反
    assert {MEMBER_A, MEMBER_B} <= ids


def test_dispatcher_list_is_the_only_home_for_dispatchers():
    """dispatcher 只有一个列表出口，避免两处口径打架。"""
    db, _client_agents = _prepare()
    app = FastAPI()
    from app.api import dispatchers as dispatchers_api

    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: make_user(OWNER_ID)
    client = TestClient(app)

    response = client.get("/api/enterprise/dispatchers", params={"tenant_id": TENANT})

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [DISPATCHER_ID]