"""调度入口被分享出去以后，访客那一侧仍然只按配置路由（方案 §9 风险 2）。

分享链路有两处会把身份钉死（``_apply_share_scope`` / ``_ensure_chat_agent_available``）：

- ``agent_id`` 钉成分享时锁定的那一个 → 访客不能自己换员工；
- ``model_config_id`` 钉成分享时选的那一个 → 访客不能自己换模型。

调度入口是**唯一一个「不自己回答、把问题转给别人跑」的 Agent**，所以这两条钉死会
被带进子 turn 才算真的生效。真实的风险是反过来的：

1. 分享钉死的 ``model_config_id`` **不能**劫持子 turn —— 子 turn 的模型按
   「成员自己配了就用成员的，没配才用调度入口的」解析（方案 §3），
   否则分享者选的模型会静默覆盖所有成员的模型，谁配谁付费的口径就断了。
2. ``agent_id`` 的钉死**不能**泄漏到子请求里 —— 子请求的 agent 必须是成员，
   否则子会话会变成「调度入口跟自己对话」，路由就成了空转。
3. 钉到别的员工的分享链接，**不能**借着 dispatcher 的会话打开调度入口。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
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

from app.api.chat import _apply_share_scope, _ensure_chat_agent_available  # noqa: E402
from app.db.models import AgentProfile, ChatSession, Message  # noqa: E402
from app.dispatch.schema import DispatchRouteDecision  # noqa: E402
from app.dispatch.service import replace_members  # noqa: E402
from app.dispatch.stream import DispatchTurnRunner  # noqa: E402
from app.security.auth import ShareScope  # noqa: E402
from app.session.session_schema import ChatTurnRequest  # noqa: E402

SHARE_MODEL = "model_share"
GUEST_ID = "share_guest_1"


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    dispatcher = make_dispatcher()
    session.add(dispatcher)
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    # 分享者在创建链接时选的是**另一个**模型：只有它没被带进子 turn，
    # 「子 turn 按配置解析模型」这条才是真的生效
    session.add(make_model_config(SHARE_MODEL, user_id=OWNER_ID))
    session.commit()
    bind_model(session, DISPATCHER_ID, "model_dispatch")
    session.commit()
    replace_members(
        session,
        dispatcher,
        [member_input(MEMBER_A, rule="入职"), member_input(MEMBER_B, rule="IT")],
    )
    session.commit()
    yield session
    session.close()


def _scope(agent_id: str = DISPATCHER_ID) -> ShareScope:
    return ShareScope(
        share_id="agentshare_dispatch",
        share_token="tok",
        tenant_id=TENANT,
        agent_id=agent_id,
        model_config_id=SHARE_MODEL,
        visitor_id=GUEST_ID,
    )


def _guest():
    """分享访客的身份仍是普通 User 行（会话隔离靠它），没有成员关系。"""
    return make_user(GUEST_ID)


def _request(**overrides) -> ChatTurnRequest:
    payload: dict[str, Any] = {
        "tenant_id": TENANT,
        "session_id": "session_share_parent",
        "agent_id": DISPATCHER_ID,
        "user_id": GUEST_ID,
        "client_turn_id": "turn_share_1",
        "message": "我要办入职",
        "channel": "web",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)


def _parent(db) -> ChatSession:
    existing = db.get(ChatSession, "session_share_parent")
    if existing is not None:
        return existing
    parent = ChatSession(
        id="session_share_parent",
        tenant_id=TENANT,
        user_id=GUEST_ID,
        agent_id=DISPATCHER_ID,
    )
    db.add(parent)
    db.commit()
    return parent


class _RouterStub:
    def __init__(self, decision: DispatchRouteDecision) -> None:
        self.decision = decision

    def decide(self, **kwargs: Any) -> DispatchRouteDecision:
        return self.decision


def _run_routed_turn(db) -> tuple[dict[str, Any], list[ChatTurnRequest]]:
    """跑一轮「访客提问 → 路由到 MEMBER_A」，返回结果与子请求。"""
    captured: list[ChatTurnRequest] = []

    def _stream(
        child_request: ChatTurnRequest,
        *,
        parent_session_id: str,
        parent_turn_id: str = "",
        parent_client_turn_id: str | None = None,
    ) -> str:
        captured.append(child_request)
        return "需要准备身份证和学历证明。"

    runner = DispatchTurnRunner(emit=lambda name, payload: None, session_factory=lambda: db)
    runner._router = _RouterStub(
        DispatchRouteDecision(
            action="route",
            member_agent_id=MEMBER_A,
            confidence=0.93,
            intent="办理入职",
            query="我想办入职，需要什么材料",
        )
    )
    runner._stream_child_turn = _stream

    parent = _parent(db)
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    # 访客请求先经过分享层：agent_id / model_config_id 都被钉死
    request = _apply_share_scope(_request(), _scope())
    result = runner.run(db, request, parent, dispatcher, parent_turn_id="msg_share_turn_1")
    return result, captured


# ---------------------------------------------------------------- 分享访问调度入口


def test_share_scope_can_open_dispatcher_session(db) -> None:
    """分享时锁的就是调度入口 → 访客能进；这与普通员工的判定口径一致。"""
    row = _ensure_chat_agent_available(db, TENANT, DISPATCHER_ID, _guest(), _scope())
    assert row.id == DISPATCHER_ID


def test_share_pinned_to_another_agent_cannot_open_dispatcher(db) -> None:
    """分享链接锁的是别的员工 → 不能拿它开调度入口的会话。"""
    with pytest.raises(HTTPException) as excinfo:
        _ensure_chat_agent_available(db, TENANT, DISPATCHER_ID, _guest(), _scope(MEMBER_A))
    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------- 子 turn 不受分享钉死影响


def test_child_turn_uses_member_model_not_share_model(db) -> None:
    """成员自己配了模型 → 子 turn 用成员的，分享钉死的模型不许劫持。"""
    _result, captured = _run_routed_turn(db)

    assert len(captured) == 1
    assert captured[0].model_config_id == "model_hr"
    assert captured[0].model_config_id != SHARE_MODEL
    # 分享钉死的模型仍留在父请求上（父会话那一层由它兜底），只是没被带下去
    assert _apply_share_scope(_request(), _scope()).model_config_id == SHARE_MODEL


def test_child_turn_agent_is_member_not_dispatcher(db) -> None:
    """子请求的 agent 必须是成员；钉死的 agent_id 泄漏下去就成了自己转给自己。"""
    _result, captured = _run_routed_turn(db)

    assert captured[0].agent_id == MEMBER_A
    assert captured[0].agent_id != DISPATCHER_ID


def test_child_session_is_not_the_shared_session(db) -> None:
    """子会话独立：访客看不到子会话，父会话只留一份答案快照。"""
    _result, captured = _run_routed_turn(db)

    assert captured[0].session_id != "session_share_parent"
    child = db.get(ChatSession, captured[0].session_id)
    assert child is not None
    assert child.agent_id == MEMBER_A

    parent_replies = db.exec(
        select(Message).where(
            Message.session_id == "session_share_parent", Message.role == "assistant"
        ).order_by(Message.created_at)
    ).all()
    # 两条：① 路由的「已转交给 X」通知（metadata.dispatch_notice） ② 成员答案
    assert len(parent_replies) == 2
    notice, answer = parent_replies
    assert notice.metadata_json["dispatch_notice"]["member_name"] == "入职小助手"
    assert answer.metadata_json["dispatch"]["member_name"] == "入职小助手"


def test_share_turn_never_touches_free_quota_path(db) -> None:
    """分享访客 + 调度入口：子 turn 带着显式 model_config_id，免费配额链路不参与。

    ``chat.py`` 的免费配额闸门只在 ``not request.model_config_id`` 时才跑，
    所以「子请求必须带 model_config_id」这条同时是性能与合规底线。
    """
    _result, captured = _run_routed_turn(db)

    assert captured[0].model_config_id, "子请求必须显式带模型，否则会落到免费回落链"