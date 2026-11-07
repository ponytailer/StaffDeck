"""配置页「试跑规则」端点（POST /api/enterprise/dispatchers/preview）。

这个端点只服务配置页，但它踩的坑和生产路由完全一样，所以断言重点放在：
1. **不落库**：试跑前后 `AgentProfile` / `DispatcherMember` 行数不变；
2. 成员数边界与生产保存同口径（<2 / >3 都拒）——1 个成员时路由比分恒 100%，
   放过去会给用户「规则配得挺好」的假信号；
3. 入参成员关系约束：禁用别的 dispatcher 作成员、禁重复、不可见成员拒；
4. 模型不可用时走兜底而不是 500（配置页可能还没选模型）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import func, select

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    OTHER_ID,
    OWNER_ID,
    TENANT,
    make_agent,
    make_model_config,
    make_user,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.api import dispatchers as dispatchers_api  # noqa: E402
from app.db import get_session  # noqa: E402
from app.db.models import AgentProfile, DispatcherMember  # noqa: E402
from app.dispatch.router import DispatcherRouter  # noqa: E402
from app.dispatch.schema import DispatchRouteDecision  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402

BASE = "/api/enterprise/dispatchers"


def _client(db, user):
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _prepare(db):
    seed_tenant(db)
    seed_two_members(db)
    db.add(make_agent(MEMBER_C, name="财务报销", description="报销、发票、打款", owner_id=OWNER_ID))
    db.add(make_model_config("model_preview", user_id=OWNER_ID))
    db.commit()
    return _client(db, make_user(OWNER_ID))


def _payload(members, **overrides):
    payload = {
        "tenant_id": TENANT,
        "message": "我要报一笔差旅费，发票怎么提交",
        "members": members,
        "config": {"fallback_mode": "clarify"},
    }
    payload.update(overrides)
    return payload


def _two_members():
    return [
        {"member_agent_id": MEMBER_A, "routing_rule": "报销、发票、打款", "example_queries": ["报销怎么提交"]},
        {"member_agent_id": MEMBER_B, "routing_rule": "合同、法务合规", "example_queries": ["合同要审多久"]},
    ]


# ---------------------------------------------------------------- 不落库


def test_preview_does_not_persist(monkeypatch) -> None:
    """试跑绝不能留下入口或成员行——配置页用户随时可能点取消。"""
    db = memory_session()
    client = _prepare(db)
    monkeypatch.setattr(
        DispatcherRouter,
        "decide",
        lambda self, **kwargs: DispatchRouteDecision(
            action="route", member_agent_id=MEMBER_A, confidence=0.9, intent="报销"
        ),
    )
    before_agents = db.exec(select(func.count()).select_from(AgentProfile)).one()
    before_members = db.exec(select(func.count()).select_from(DispatcherMember)).one()

    response = client.post(f"{BASE}/preview", json=_payload(_two_members()))
    assert response.status_code == 200

    after_agents = db.exec(select(func.count()).select_from(AgentProfile)).one()
    after_members = db.exec(select(func.count()).select_from(DispatcherMember)).one()
    assert (before_agents, before_members) == (after_agents, after_members)


def test_preview_returns_member_name(monkeypatch) -> None:
    """前端要显示「转给 小王」，只给 member_agent_id 不够用。"""
    db = memory_session()
    client = _prepare(db)
    monkeypatch.setattr(
        DispatcherRouter,
        "decide",
        lambda self, **kwargs: DispatchRouteDecision(
            action="route", member_agent_id=MEMBER_A, confidence=0.88, intent="报销"
        ),
    )
    body = client.post(f"{BASE}/preview", json=_payload(_two_members())).json()
    assert body["decision"]["action"] == "route"
    assert body["member_name"]
    assert len(body["candidates"]) == 2
    assert {item["agent_id"] for item in body["candidates"]} == {MEMBER_A, MEMBER_B}


# ---------------------------------------------------------------- 数量边界


def test_preview_rejects_single_member() -> None:
    db = memory_session()
    client = _prepare(db)
    response = client.post(
        f"{BASE}/preview",
        json=_payload([{"member_agent_id": MEMBER_A, "routing_rule": "报销"}]),
    )
    assert response.status_code == 400
    assert "试跑" in response.json()["detail"]


def test_preview_rejects_too_many_members() -> None:
    db = memory_session()
    client = _prepare(db)
    members = [
        {"member_agent_id": MEMBER_A, "routing_rule": "x"},
        {"member_agent_id": MEMBER_B, "routing_rule": "y"},
        {"member_agent_id": MEMBER_C, "routing_rule": "z"},
        {"member_agent_id": OTHER_ID, "routing_rule": "w"},
    ]
    response = client.post(f"{BASE}/preview", json=_payload(members))
    assert response.status_code == 400
    assert "最多" in response.json()["detail"]


# ---------------------------------------------------------------- 关系约束


def test_preview_rejects_duplicate_member() -> None:
    db = memory_session()
    client = _prepare(db)
    members = [
        {"member_agent_id": MEMBER_A, "routing_rule": "x"},
        {"member_agent_id": MEMBER_A, "routing_rule": "y"},
    ]
    response = client.post(f"{BASE}/preview", json=_payload(members))
    assert response.status_code == 400
    assert "一次" in response.json()["detail"]


def test_preview_rejects_invisible_member() -> None:
    """别人名下没公开的员工不能拿来试跑——否则等于绕开可见性调它。"""
    db = memory_session()
    client = _prepare(db)
    db.add(make_agent("agent_private", name="私有员工", owner_id=OTHER_ID))
    db.commit()
    members = [
        {"member_agent_id": MEMBER_A, "routing_rule": "x"},
        {"member_agent_id": "agent_private", "routing_rule": "y"},
    ]
    response = client.post(f"{BASE}/preview", json=_payload(members))
    assert response.status_code == 403


def test_preview_rejects_dispatcher_member() -> None:
    """禁嵌套：把另一个调度入口当成员要拒。"""
    db = memory_session()
    client = _prepare(db)
    from dispatch_fixtures import make_dispatcher

    db.add(make_dispatcher("agent_other_disp", owner_id=OWNER_ID))
    db.commit()
    members = [
        {"member_agent_id": MEMBER_A, "routing_rule": "x"},
        {"member_agent_id": "agent_other_disp", "routing_rule": "y"},
    ]
    response = client.post(f"{BASE}/preview", json=_payload(members))
    assert response.status_code == 400
    assert "嵌套" in response.json()["detail"]


def test_preview_rejects_self_reference() -> None:
    db = memory_session()
    client = _prepare(db)
    members = [
        {"member_agent_id": MEMBER_A, "routing_rule": "x"},
        {"member_agent_id": "agent_self", "routing_rule": "y"},
    ]
    response = client.post(
        f"{BASE}/preview", json=_payload(members, dispatcher_id="agent_self")
    )
    assert response.status_code == 400
    assert "自己" in response.json()["detail"]


def test_preview_rejects_empty_message() -> None:
    db = memory_session()
    client = _prepare(db)
    response = client.post(f"{BASE}/preview", json=_payload(_two_members(), message="   "))
    assert response.status_code == 400


# ---------------------------------------------------------------- 兜底友好


def test_preview_without_any_model_does_not_500() -> None:
    """配置页第一段还没选模型就点试跑 → 走兜底话术，不能 500。"""
    db = memory_session()
    seed_tenant(db)
    seed_two_members(db)
    db.commit()
    client = _client(db, make_user(OWNER_ID))

    response = client.post(f"{BASE}/preview", json=_payload(_two_members()))
    assert response.status_code == 200
    body = response.json()
    # 没有模型时 DispatcherRouter 的 raw 为 None → _normalize 走 fallback
    assert body["decision"]["action"] in {"clarify", "reply"}