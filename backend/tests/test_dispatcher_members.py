"""调度入口的成员配置与配置类校验（api/dispatchers.py + dispatch/service.py）。

关注四件事：
1. 数量边界：<2 与 >3 都要被拒（创建与更新一视同仁），且**不留半成品入口**；
2. 关系约束：禁自引、禁嵌套（成员是另一个 dispatcher）、禁重复、跨租户/停用拒绝；
3. 整体替换语义：PUT 是「全量提交」，重复提交幂等、不残留旧行；
4. 级联：删员工后成员行双向清理，反查接口同步正确。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import func, select

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    MEMBER_C,
    OTHER_ID,
    OWNER_ID,
    TENANT,
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
from app.api.agents import delete_agent  # noqa: E402
from app.db import get_session  # noqa: E402
from app.db.models import AgentProfile, DispatcherMember  # noqa: E402
from app.dispatch.schema import DispatchConfig, DispatcherMemberInput  # noqa: E402
from app.dispatch.service import (  # noqa: E402
    DispatchValidationError,
    list_dispatchers_for_member,
    replace_members,
    validate_member_inputs,
)
from app.security.auth import get_current_user  # noqa: E402

BASE = "/api/enterprise/dispatchers"


def _client(db, user):
    app = FastAPI()
    app.include_router(dispatchers_api.router)
    app.dependency_overrides[get_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return _JsonClient(TestClient(app), app)


class _JsonClient:
    """把 pydantic 对象（成员配置）自动转成 dict 的 TestClient 包装。

    ``TestClient(json=...)`` 用标准 json.dumps，不接受 pydantic 实例；
    在每个用例里手工 model_dump 只会把测试写噪，这里统一兜一次。
    """

    def __init__(self, client: TestClient, app: FastAPI) -> None:
        self._client = client
        self.app = app

    @staticmethod
    def _clean(value):
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if isinstance(value, dict):
            return {key: _JsonClient._clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_JsonClient._clean(item) for item in value]
        return value

    def __getattr__(self, name):
        method = getattr(self._client, name)
        if name not in {"post", "put", "patch"}:
            return method

        def wrapped(*args, **kwargs):
            if "json" in kwargs:
                kwargs["json"] = self._clean(kwargs["json"])
            return method(*args, **kwargs)

        return wrapped


def _prepare(db):
    seed_tenant(db)
    seed_two_members(db)
    db.add(
        make_agent(MEMBER_C, name="财务报销", description="报销、发票、打款", owner_id=OWNER_ID)
    )
    db.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    db.commit()
    return _client(db, make_user(OWNER_ID))


def _create_payload(members, **overrides):
    payload = {
        "tenant_id": TENANT,
        "name": "前台调度",
        "model_config_id": "model_dispatch",
        # TestClient 的 json= 走标准 json.dumps，pydantic 对象由 _JsonClient 兜
        "members": list(members),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- 数量边界


def test_create_rejects_single_member() -> None:
    """只有 1 个成员时没有「选谁」的判断，直接建普通员工更合适。"""
    db = memory_session()
    client = _prepare(db)
    response = client.post(BASE, json=_create_payload([member_input(MEMBER_A, rule="入职")]))
    assert response.status_code == 400, response.text
    assert "至少要有 2 个同事" in response.json()["detail"]
    # 不留半成品：库里不该出现同名入口
    assert db.exec(select(AgentProfile).where(AgentProfile.name == "前台调度")).first() is None


def test_create_rejects_zero_members() -> None:
    db = memory_session()
    client = _prepare(db)
    response = client.post(BASE, json=_create_payload([]))
    assert response.status_code == 400
    assert "至少要有 2 个同事" in response.json()["detail"]


def test_create_rejects_more_than_three_members() -> None:
    db = memory_session()
    client = _prepare(db)
    db.add(make_agent("agent_extra", name="第四个同事", owner_id=OWNER_ID))
    db.commit()
    response = client.post(
        BASE,
        json=_create_payload(
            [
                member_input(MEMBER_A),
                member_input(MEMBER_B),
                member_input(MEMBER_C),
                member_input("agent_extra"),
            ]
        ),
    )
    assert response.status_code == 400
    assert "最多只能添加 3 个同事" in response.json()["detail"]


def test_create_accepts_two_and_three_members() -> None:
    db = memory_session()
    client = _prepare(db)
    two = client.post(BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)]))
    assert two.status_code == 200, two.text
    assert len(two.json()["members"]) == 2

    db.add(make_agent("agent_extra", name="第四个同事", owner_id=OWNER_ID))
    db.commit()
    three = client.post(
        BASE,
        json=_create_payload(
            [
                member_input(MEMBER_A),
                member_input(MEMBER_B),
                member_input(MEMBER_C),
            ],
            name="第二个调度",
        ),
    )
    assert three.status_code == 200, three.text
    assert len(three.json()["members"]) == 3


# ---------------------------------------------------------------- 关系约束


def test_create_rejects_self_reference() -> None:
    db = memory_session()
    _prepare(db)
    with pytest.raises(DispatchValidationError) as excinfo:
        validate_member_inputs(
            db, TENANT, DISPATCHER_ID, [member_input(DISPATCHER_ID), member_input(MEMBER_A)]
        )
    assert "不能把自己设为成员" in str(excinfo.value)


def test_create_rejects_duplicate_member() -> None:
    db = memory_session()
    _prepare(db)
    with pytest.raises(DispatchValidationError) as excinfo:
        validate_member_inputs(
            db, TENANT, DISPATCHER_ID, [member_input(MEMBER_A), member_input(MEMBER_A)]
        )
    assert "只能添加一次" in str(excinfo.value)


def test_create_rejects_nested_dispatcher() -> None:
    """禁环：不能把另一个调度入口设为成员（天然封死嵌套）。"""
    db = memory_session()
    _prepare(db)
    db.add(make_dispatcher("agent_dispatcher_2", name="另一个调度"))
    db.commit()
    with pytest.raises(DispatchValidationError) as excinfo:
        validate_member_inputs(
            db, TENANT, DISPATCHER_ID, [member_input(MEMBER_A), member_input("agent_dispatcher_2")]
        )
    assert "不支持嵌套调度" in str(excinfo.value)


def test_create_rejects_cross_tenant_member() -> None:
    db = memory_session()
    _prepare(db)
    db.add(make_agent("agent_foreign", name="别的租户", tenant_id="tenant_other"))
    db.commit()
    with pytest.raises(DispatchValidationError) as excinfo:
        validate_member_inputs(
            db, TENANT, DISPATCHER_ID, [member_input(MEMBER_A), member_input("agent_foreign")]
        )
    assert "不存在或已停用" in str(excinfo.value)


def test_create_rejects_archived_member() -> None:
    db = memory_session()
    _prepare(db)
    db.add(make_agent("agent_archived", name="已归档同事", status="archived"))
    db.commit()
    with pytest.raises(DispatchValidationError) as excinfo:
        validate_member_inputs(
            db, TENANT, DISPATCHER_ID, [member_input(MEMBER_A), member_input("agent_archived")]
        )
    assert "不存在或已停用" in str(excinfo.value)


def test_create_rejects_invisible_member() -> None:
    """别人名下没公开的员工不能被挂到自己入口上（等于绕开可见性调用它）。"""
    db = memory_session()
    client = _prepare(db)
    db.add(make_agent("agent_private", name="别人的私有员工", owner_id=OTHER_ID))
    db.commit()
    response = client.post(
        BASE,
        json=_create_payload([member_input(MEMBER_A), member_input("agent_private")]),
    )
    assert response.status_code == 403, response.text
    assert "看不见的同事" in response.json()["detail"]


# ---------------------------------------------------------------- 更新路径


def test_update_rejects_dropping_below_two_members() -> None:
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]

    response = client.put(
        f"{BASE}/{agent_id}",
        json={"tenant_id": TENANT, "members": [member_input(MEMBER_A).model_dump()]},
    )
    assert response.status_code == 400, response.text
    assert "至少要有 2 个同事" in response.json()["detail"]
    # 校验在删除之前，原配置不受损
    rows = db.exec(
        select(DispatcherMember).where(DispatcherMember.dispatcher_agent_id == agent_id)
    ).all()
    assert len(rows) == 2


def test_update_replaces_members_idempotently() -> None:
    """PUT 是全量提交语义：重复提交不叠加，换人后旧行不残留。"""
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    )
    agent_id = created.json()["id"]
    payload = {
        "tenant_id": TENANT,
        "members": [
            member_input(MEMBER_B, rule="电脑网络", sort_order=0),
            member_input(MEMBER_C, rule="报销打款", sort_order=1),
        ],
    }
    first = client.put(f"{BASE}/{agent_id}", json=payload)
    second = client.put(f"{BASE}/{agent_id}", json=payload)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert [m["member_agent_id"] for m in second.json()["members"]] == [MEMBER_B, MEMBER_C]
    total = db.exec(
        select(func.count())
        .select_from(DispatcherMember)
        .where(DispatcherMember.dispatcher_agent_id == agent_id)
    ).one()
    assert int(total) == 2


def test_replace_members_keeps_config_on_validation_error() -> None:
    db = memory_session()
    _prepare(db)
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()

    with pytest.raises(DispatchValidationError):
        replace_members(db, dispatcher, [member_input(MEMBER_A)])
    db.rollback()
    rows = db.exec(
        select(DispatcherMember).where(DispatcherMember.dispatcher_agent_id == dispatcher.id)
    ).all()
    assert len(rows) == 2


# ---------------------------------------------------------------- 级联与反查


def test_deleting_member_agent_requires_unbinding_first() -> None:
    """员工在某个员工小队里当成员时**不许直接删**，必须先解绑。

    旧行为是「删员工 → 静默摘掉各入口的成员行」，等于替入口主人改了配置还不告诉他：
    少一个成员后路由分叉会变，极端情况跌到 1 个甚至 0 个成员、问题全走兜底。
    现在改成 409 + 把引用它的入口名字带回去。
    """
    db = memory_session()
    _prepare(db)
    admin = make_user("user_admin", role="admin")
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.add(make_dispatcher("agent_dispatcher_2", name="第二个调度"))
    replace_members(
        db, db.get(AgentProfile, "agent_dispatcher_2"), [member_input(MEMBER_A), member_input(MEMBER_C)]
    )
    db.commit()

    assert set(list_dispatchers_for_member(db, TENANT, MEMBER_A)) == {
        DISPATCHER_ID,
        "agent_dispatcher_2",
    }

    # 还在被引用 → 409，且报错里说清楚是哪几个入口
    with pytest.raises(HTTPException) as excinfo:
        delete_agent(agent_id=MEMBER_A, tenant_id=TENANT, db=db, current_user=admin)
    assert excinfo.value.status_code == 409
    detail = str(excinfo.value.detail)
    assert "2 个员工小队" in detail
    assert "前台调度" in detail and "第二个调度" in detail
    # 401 之后员工必须还在（不能留半删状态）
    db.rollback()
    assert db.get(AgentProfile, MEMBER_A) is not None
    assert set(list_dispatchers_for_member(db, TENANT, MEMBER_A)) == {
        DISPATCHER_ID,
        "agent_dispatcher_2",
    }


def test_deleting_member_agent_succeeds_after_unbinding() -> None:
    """解绑（把自己的成员行摘掉）之后就能正常删了。"""
    db = memory_session()
    _prepare(db)
    admin = make_user("user_admin", role="admin")
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()

    # 换掉 MEMBER_A（换成 MEMBER_C，维持 2 人下限——replace_members 本身强制下限，
    # 这里验的是删除守卫，不是成员数校验）
    replace_members(db, dispatcher, [member_input(MEMBER_B), member_input(MEMBER_C)])
    db.commit()
    assert list_dispatchers_for_member(db, TENANT, MEMBER_A) == []

    delete_agent(agent_id=MEMBER_A, tenant_id=TENANT, db=db, current_user=admin)
    db.commit()
    assert db.get(AgentProfile, MEMBER_A) is None
    assert db.get(AgentProfile, DISPATCHER_ID) is not None


def test_deleting_dispatcher_clears_its_member_rows() -> None:
    db = memory_session()
    _prepare(db)
    admin = make_user("user_admin", role="admin")
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()

    delete_agent(agent_id=DISPATCHER_ID, tenant_id=TENANT, db=db, current_user=admin)
    db.commit()
    remaining = db.exec(
        select(func.count())
        .select_from(DispatcherMember)
        .where(DispatcherMember.dispatcher_agent_id == DISPATCHER_ID)
    ).one()
    assert int(remaining) == 0
    # 员工本身没被删
    assert db.get(AgentProfile, MEMBER_A) is not None


def test_referenced_by_endpoint() -> None:
    db = memory_session()
    client = _prepare(db)
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    replace_members(db, dispatcher, [member_input(MEMBER_A), member_input(MEMBER_B)])
    db.commit()

    response = client.get(f"{BASE}/{MEMBER_A}/referenced-by", params={"tenant_id": TENANT})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1
    assert body["dispatchers"] == [{"id": DISPATCHER_ID, "name": "前台调度"}]

    none = client.get(f"{BASE}/{MEMBER_C}/referenced-by", params={"tenant_id": TENANT})
    assert none.json() == {"count": 0, "dispatchers": []}


# ---------------------------------------------------------------- 配置读写


def test_config_roundtrip() -> None:
    db = memory_session()
    client = _prepare(db)
    config = {
        "fallback_mode": "reply",
        "fallback_reply": "这个问题请联系前台。",
        "offer_candidates": False,
        "min_confidence": 0.7,
    }
    created = client.post(
        BASE,
        json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)], config=config),
    )
    assert created.status_code == 200, created.text
    assert created.json()["config"]["fallback_mode"] == "reply"
    assert created.json()["config"]["min_confidence"] == 0.7

    detail = client.get(f"{BASE}/{created.json()['id']}", params={"tenant_id": TENANT})
    assert detail.json()["config"]["fallback_reply"] == "这个问题请联系前台。"


def test_avatar_preset_roundtrip() -> None:
    """小队头像：创建写入、详情回读、PUT 不传送保留 / 传空串清空。

    头像 key 落在 `AgentProfile.metadata_json["avatar_preset"]`，前端 6 个
    预设共用这一列；历史数据没有这个键时返回 None，由前端回落默认图形。
    """
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE,
        json=_create_payload(
            [member_input(MEMBER_A), member_input(MEMBER_B)], avatar_preset="orbit"
        ),
    )
    assert created.status_code == 200, created.text
    dispatcher_id = created.json()["id"]
    assert created.json()["avatar_preset"] == "orbit"

    detail = client.get(f"{BASE}/{dispatcher_id}", params={"tenant_id": TENANT})
    assert detail.json()["avatar_preset"] == "orbit"

    # 不传 = 沿用现有值（成员编辑弹窗只改成员时不该把头像抹掉）
    kept = client.put(
        f"{BASE}/{dispatcher_id}",
        json={
            "tenant_id": TENANT,
            "members": [member_input(MEMBER_A), member_input(MEMBER_C)],
        },
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["avatar_preset"] == "orbit"

    # 传空串 = 清掉，回落默认图形
    cleared = client.put(
        f"{BASE}/{dispatcher_id}",
        json={"tenant_id": TENANT, "avatar_preset": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["avatar_preset"] is None


def test_dirty_config_falls_back_to_defaults() -> None:
    """手改库里脏数据不许把路由链路炸掉。"""
    from app.dispatch.service import dispatch_config

    row = make_agent(DISPATCHER_ID, name="脏数据入口", kind="dispatcher")
    row.metadata_json = {"dispatch_config": {"fallback_mode": "不存在的模式"}}
    assert (row.metadata_json or {}) != {}
    config = dispatch_config(row)
    assert isinstance(config, DispatchConfig)
    assert config.fallback_mode == "clarify"


def test_member_read_exposes_own_model_flag() -> None:
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    )
    assert created.status_code == 200, created.text
    members = {m["member_agent_id"]: m for m in created.json()["members"]}
    # 夹具里两个员工都绑了自己的模型
    assert members[MEMBER_A]["has_own_model"] is True
    assert members[MEMBER_A]["agent_name"] == "入职小助手"

    db.add(make_agent("agent_bare", name="没配模型的同事", owner_id=OWNER_ID))
    db.commit()
    updated = client.put(
        f"{BASE}/{created.json()['id']}",
        json={
            "tenant_id": TENANT,
            "members": [member_input(MEMBER_A), member_input("agent_bare")],
        },
    )
    assert updated.status_code == 200, updated.text
    bare = {m["member_agent_id"]: m for m in updated.json()["members"]}["agent_bare"]
    assert bare["has_own_model"] is False


def test_deleted_member_shows_placeholder_name() -> None:
    """成员行还在但员工被硬删（历史脏数据）→ 读接口不许 500。"""
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    )
    agent_id = created.json()["id"]
    gone = db.get(AgentProfile, MEMBER_B)
    db.delete(gone)
    db.commit()

    detail = client.get(f"{BASE}/{agent_id}", params={"tenant_id": TENANT})
    assert detail.status_code == 200, detail.text
    names = {m["member_agent_id"]: m["agent_name"] for m in detail.json()["members"]}
    assert names[MEMBER_B] == "（已删除）"


def test_non_dispatcher_agent_is_404() -> None:
    db = memory_session()
    client = _prepare(db)
    response = client.get(f"{BASE}/{MEMBER_A}", params={"tenant_id": TENANT})
    assert response.status_code == 404
    assert response.json()["detail"] == "Dispatcher not found"


def test_list_only_returns_dispatchers() -> None:
    db = memory_session()
    client = _prepare(db)
    dispatcher = make_dispatcher()
    db.add(dispatcher)
    db.commit()
    response = client.get(BASE, params={"tenant_id": TENANT})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [DISPATCHER_ID]


def test_delete_dispatcher_endpoint() -> None:
    db = memory_session()
    client = _prepare(db)
    created = client.post(
        BASE, json=_create_payload([member_input(MEMBER_A), member_input(MEMBER_B)])
    )
    agent_id = created.json()["id"]
    response = client.delete(f"{BASE}/{agent_id}", params={"tenant_id": TENANT})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "deleted"}
    assert db.get(AgentProfile, agent_id) is None
    # 成员本体保留
    assert db.get(AgentProfile, MEMBER_A) is not None


def test_member_input_defaults() -> None:
    item = DispatcherMemberInput(member_agent_id=MEMBER_A)
    assert item.routing_rule == ""
    assert item.example_queries == []
    assert item.sort_order == 0


def test_http_exception_type_is_preserved() -> None:
    """校验错误必须是 400 而不是 500（防止未来把 ValidateError 换成 Exception）。"""
    db = memory_session()
    client = _prepare(db)
    response = client.post(BASE, json=_create_payload([member_input(MEMBER_A)]))
    assert isinstance(response.status_code, int)
    assert response.status_code == 400
    with pytest.raises(HTTPException):  # sanity: 直接调用时抛的是 HTTPException
        dispatchers_api._ensure_model(db, TENANT, None)