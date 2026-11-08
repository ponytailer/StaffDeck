"""「我的小队」页概览接口（api/dispatchers.py 的 /overview）。

三条口径必须钉住：
1. **路由顺序**：`GET /dispatchers/overview` 不能被 `/{agent_id}` 抢走当 id 用；
2. **统计口径**：transfers 是各会话 dispatch_log 条数之和（每会话上限 10 → 是下限），
   sessions_7d 用会话 updated_at 近似；
3. **健康度只算真问题**：缺路由规则 / 模型已被删或停用 / 只有 1 位成员；
   「未发布到广场」不算问题。
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    ADMIN_ID,
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
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
from app.db.models import ChatSession, utc_now  # noqa: E402
from app.dispatch.service import DISPATCH_LOG_KEY, replace_members  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402

BASE = "/api/enterprise/dispatchers"
OVERVIEW = f"{BASE}/overview"


def _client(db, user=None):
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user or make_user(OWNER_ID)
    return TestClient(app)


def _seed_squad(db, *, model_id: str = "model_dispatch"):
    seed_tenant(db)
    seed_two_members(db)
    db.add(make_model_config(model_id, user_id=OWNER_ID))
    row = make_dispatcher(DISPATCHER_ID)
    db.add(row)
    db.commit()
    replace_members(
        db,
        row,
        [
            member_input(MEMBER_A, rule="入职、离职、考勤异常", sort_order=0),
            member_input(MEMBER_B, rule="电脑、网络、账号权限", sort_order=1),
        ],
    )
    db.commit()
    return row


def _session(
    db,
    session_id: str,
    *,
    agent_id: str = DISPATCHER_ID,
    days_ago: float = 0,
    log: list[dict] | None = None,
) -> None:
    stamp = utc_now() - timedelta(days=days_ago)
    db.add(
        ChatSession(
            id=session_id,
            tenant_id=TENANT,
            user_id=OWNER_ID,
            agent_id=agent_id,
            context_state_json={DISPATCH_LOG_KEY: log or []},
            created_at=stamp,
            updated_at=stamp,
        )
    )


def test_overview_is_not_swallowed_by_agent_id_route() -> None:
    """路由顺序回归：/overview 必须命中概览，而不是被当成 agent_id 查详情。"""
    db = memory_session()
    _seed_squad(db)
    resp = _client(db).get(OVERVIEW, params={"tenant_id": TENANT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["squads"] == 1
    assert "activity" in body


def test_overview_empty_tenant_is_zero_not_error() -> None:
    db = memory_session()
    seed_tenant(db)
    resp = _client(db).get(OVERVIEW, params={"tenant_id": TENANT})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "squads": 0,
        "members": 0,
        "transfers": 0,
        "published": 0,
        "missing_rule_members": 0,
        "broken_models": 0,
        "single_member_squads": 0,
        "activity": [],
    }


def test_overview_aggregates_transfers_and_top_members() -> None:
    db = memory_session()
    _seed_squad(db)
    # 近 7 天：两次转给 A、一次转给 B
    _session(
        db,
        "sess_recent",
        days_ago=1,
        log=[
            {"member_name": "入职小助手", "user_query": "入职材料"},
            {"member_name": "入职小助手", "user_query": "工牌怎么办"},
            {"member_name": "IT 支持", "user_query": "连不上网"},
        ],
    )
    # 30 天前：一次转给 B，不计入 sessions_7d，但 transfers 仍累计
    _session(db, "sess_old", days_ago=30, log=[{"member_name": "IT 支持"}])
    db.commit()

    body = _client(db).get(OVERVIEW, params={"tenant_id": TENANT}).json()
    activity = body["activity"][0]
    assert body["transfers"] == 4
    assert activity["sessions"] == 2
    assert activity["sessions_7d"] == 1
    assert activity["transfers"] == 4
    # 次数相同时按名字升序（确定性排序，避免每次请求顺序抖动）
    assert activity["top_members"] == [
        {"name": "IT 支持", "count": 2},
        {"name": "入职小助手", "count": 2},
    ]


def test_overview_survives_dirty_context_state() -> None:
    """手改库里的脏 JSON 不许把页面炸掉（dispatch_log 不是 list / 条目不是 dict）。"""
    db = memory_session()
    _seed_squad(db)
    _session(db, "sess_dict", log=[])  # 占位，下面直接改脏
    dirty = db.get(ChatSession, "sess_dict")
    assert dirty is not None
    dirty.context_state_json = {DISPATCH_LOG_KEY: "not-a-list"}
    db.add(dirty)
    broken = ChatSession(
        id="sess_broken",
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={DISPATCH_LOG_KEY: ["oops", 3, None]},
    )
    db.add(broken)
    db.commit()

    resp = _client(db).get(OVERVIEW, params={"tenant_id": TENANT})
    assert resp.status_code == 200, resp.text
    activity = resp.json()["activity"][0]
    assert activity["sessions"] == 2
    assert activity["transfers"] == 0
    assert activity["top_members"] == []


def test_overview_health_flags_missing_rule_and_broken_model() -> None:
    db = memory_session()
    squad = _seed_squad(db)
    # 成员 B 的规则清空 → 缺规则
    replace_members(
        db,
        squad,
        [
            member_input(MEMBER_A, rule="入职、离职", sort_order=0),
            member_input(MEMBER_B, rule="", sort_order=1),
        ],
    )
    # 绑了一个库里不存在的模型 → 这轮对话会直接失败，属真问题
    bind_model(db, DISPATCHER_ID, "model_ghost")
    # 另一个只有一个成员的小队（脏数据，绕过创建校验直接写）
    db.add(make_agent("agent_solo", name="单人小队", kind="dispatcher", owner_id=OWNER_ID))
    db.commit()

    body = _client(db).get(OVERVIEW, params={"tenant_id": TENANT}).json()
    assert body["squads"] == 2
    assert body["missing_rule_members"] >= 1
    assert body["broken_models"] == 1
    assert body["single_member_squads"] == 1
    # 未发布到广场不算问题：这条用例里一个小队都没发布，published 必须是 0 而非「待办」
    assert body["published"] == 0


def test_overview_respects_visibility_for_non_admin() -> None:
    """普通成员只看自己建的小队 —— 概览数字不能比列表多。"""
    db = memory_session()
    _seed_squad(db)
    db.add(make_agent("agent_other_squad", name="别人的入口", kind="dispatcher", owner_id="user_other"))
    db.commit()

    as_admin = _client(db, make_user(ADMIN_ID, role="admin"))
    as_member = _client(db, make_user(OWNER_ID))

    assert as_admin.get(OVERVIEW, params={"tenant_id": TENANT}).json()["squads"] == 2
    assert as_member.get(OVERVIEW, params={"tenant_id": TENANT}).json()["squads"] == 1


def test_overview_rejects_cross_tenant_probe() -> None:
    db = memory_session()
    _seed_squad(db)
    resp = _client(db).get(OVERVIEW, params={"tenant_id": "tenant_other"})
    assert resp.status_code in {403, 404}, resp.text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
