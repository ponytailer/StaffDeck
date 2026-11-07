"""取消联动：父会话点取消要能停掉正在跑的子 turn。

难点是**路由窗口**：点取消的瞬间可能还在等路由模型（1~2s），此时还没有子 turn
可以取消。这条窗口只能靠 ``dispatch_cancel_requested`` 标记覆盖——起了子 turn
之前查一次标记，命中就不再起。这里把三个时刻都固化下来。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
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
    member_input,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.db.models import AgentProfile, ChatSession  # noqa: E402
from app.dispatch.schema import DispatchRouteDecision  # noqa: E402
from app.dispatch.service import (  # noqa: E402
    ACTIVE_DISPATCH_KEY,
    DISPATCH_CANCEL_KEY,
    replace_members,
)
from app.dispatch.stream import DispatchTurnRunner  # noqa: E402
from app.session.session_schema import ChatTurnRequest  # noqa: E402

PARENT_ID = "session_parent"


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    dispatcher = make_dispatcher()
    session.add(dispatcher)
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    session.commit()
    bind_model(session, DISPATCHER_ID, "model_dispatch")
    replace_members(
        session,
        dispatcher,
        [member_input(MEMBER_A, rule="入职"), member_input(MEMBER_B, rule="IT")],
    )
    session.commit()
    yield session
    session.close()


class _Harness:
    """把 runner / 事件 / 子请求收集到一起，方便断言三个时刻。"""

    def __init__(self, db, decision: DispatchRouteDecision, *, before_child=None):
        self.db = db
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.captured: list[ChatTurnRequest] = []
        self.decision = decision
        self.cancelled_child: list[tuple[str, str]] = []
        self.before_child = before_child

        runner = DispatchTurnRunner(
            emit=lambda name, payload: self.events.append((name, payload)),
            session_factory=lambda: db,
        )
        runner._router = self
        runner._stream_child_turn = self._stream_child
        self.runner = runner

    def decide(self, **kwargs):
        if self.before_child is not None:
            self.before_child(self.db)
        return self.decision

    def _stream_child(
        self,
        child_request: ChatTurnRequest,
        *,
        parent_session_id: str,
        parent_turn_id: str = "",
        parent_client_turn_id: str | None = None,
    ) -> str:
        self.captured.append(child_request)
        return "子员工的回答"

    def run(self, *, parent: ChatSession | None = None) -> dict[str, Any]:
        parent = parent or self._ensure_parent()
        request = ChatTurnRequest(
            tenant_id=TENANT,
            session_id=PARENT_ID,
            agent_id=DISPATCHER_ID,
            user_id=OWNER_ID,
            client_turn_id="turn_1",
            message="我要办入职",
            channel="web",
        )
        return self.runner.run(self.db, request, parent, self.db.get(AgentProfile, DISPATCHER_ID))

    def _ensure_parent(self) -> ChatSession:
        parent = self.db.get(ChatSession, PARENT_ID)
        if parent is None:
            parent = ChatSession(
                id=PARENT_ID,
                tenant_id=TENANT,
                user_id=OWNER_ID,
                agent_id=DISPATCHER_ID,
            )
            self.db.add(parent)
            self.db.commit()
        return parent

    @property
    def event_names(self) -> list[str]:
        return [name for name, _ in self.events]


def _route_decision() -> DispatchRouteDecision:
    return DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9)


def _set_cancel(db, parent_id: str = PARENT_ID) -> None:
    """模拟取消端点：另一个请求把标记写进库里。"""
    parent = db.get(ChatSession, parent_id)
    metadata = dict(parent.context_state_json or {})
    metadata[DISPATCH_CANCEL_KEY] = True
    parent.context_state_json = metadata
    db.add(parent)
    db.commit()


# ---------------------------------------------------------------- 路由窗口内取消


def test_cancel_before_child_turn_skips_it(db) -> None:
    harness = _Harness(db, _route_decision(), before_child=lambda d: _set_cancel(d))
    result = harness.run()
    assert result == {"cancelled": True}
    assert harness.captured == []
    assert "stream_cancelled" in harness.event_names
    # 决策与「已转给谁」还是要发的（用户看得见发生了什么）
    assert harness.event_names[0] == "dispatch_decision"
    assert "stream_status" in harness.event_names


def test_cancel_clears_active_dispatch(db) -> None:
    harness = _Harness(db, _route_decision(), before_child=lambda d: _set_cancel(d))
    harness.run()
    parent = db.get(ChatSession, PARENT_ID)
    db.refresh(parent)
    assert ACTIVE_DISPATCH_KEY not in (parent.context_state_json or {})


# ---------------------------------------------------------------- 遗留标记


def test_leftover_flag_does_not_cancel_next_turn(db) -> None:
    """取消过一次不能把这个会话之后每一轮都取消掉。"""
    parent = ChatSession(
        id=PARENT_ID,
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={DISPATCH_CANCEL_KEY: True},
    )
    db.add(parent)
    db.commit()

    harness = _Harness(db, _route_decision())
    result = harness.run(parent=parent)
    assert result["reply"] == "子员工的回答"
    assert len(harness.captured) == 1
    db.refresh(parent)
    assert DISPATCH_CANCEL_KEY not in (parent.context_state_json or {})


def test_flag_cleared_even_when_route_fails(db) -> None:
    """路由阶段就异常时也要把标记清掉，否则用户永远取消不了下一轮。"""
    parent = ChatSession(
        id=PARENT_ID,
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={DISPATCH_CANCEL_KEY: True},
    )
    db.add(parent)
    db.commit()

    harness = _Harness(db, _route_decision())

    def _boom(**kwargs):
        raise RuntimeError("路由炸了")

    harness.runner._router = type("R", (), {"decide": staticmethod(_boom)})()
    result = harness.run(parent=parent)
    assert result["reply"]  # 有可用话术，不是空
    db.refresh(parent)
    assert DISPATCH_CANCEL_KEY not in (parent.context_state_json or {})


# ---------------------------------------------------------------- 子 turn 期间的取消


def test_cancel_flag_written_during_child_turn_is_ignored_for_this_turn(db) -> None:
    """子 turn 已经起来了，此时写标记只影响下一轮；本轮正常收尾。"""
    harness = _Harness(db, _route_decision())

    original = harness._stream_child

    def _stream_and_cancel(
        child_request,
        *,
        parent_session_id,
        parent_turn_id="",
        parent_client_turn_id=None,
    ):
        reply = original(child_request, parent_session_id=parent_session_id)
        _set_cancel(db)
        return reply

    harness.runner._stream_child_turn = _stream_and_cancel
    result = harness.run()
    assert result["reply"] == "子员工的回答"
    assert result["dispatch"]["member_agent_id"] == MEMBER_A


# ---------------------------------------------------------------- active_dispatch 生命周期


def test_active_dispatch_recorded_and_cleared(db) -> None:
    harness = _Harness(db, _route_decision())
    seen: list[dict[str, Any]] = []

    original = harness._stream_child

    def _inspect(
        child_request,
        *,
        parent_session_id,
        parent_turn_id="",
        parent_client_turn_id=None,
    ):
        parent = db.get(ChatSession, PARENT_ID)
        db.refresh(parent)
        seen.append(dict((parent.context_state_json or {}).get(ACTIVE_DISPATCH_KEY) or {}))
        return original(child_request, parent_session_id=parent_session_id)

    harness.runner._stream_child_turn = _inspect
    harness.run()

    assert seen and seen[0]["child_session_id"]
    assert seen[0]["child_turn_id"].endswith(MEMBER_A[:8])
    parent = db.get(ChatSession, PARENT_ID)
    db.refresh(parent)
    assert ACTIVE_DISPATCH_KEY not in (parent.context_state_json or {})


def test_active_dispatch_cleared_even_on_failure(db) -> None:
    harness = _Harness(db, _route_decision())

    def _boom(
        child_request,
        *,
        parent_session_id,
        parent_turn_id="",
        parent_client_turn_id=None,
    ):
        raise RuntimeError("子 turn 炸了")

    harness.runner._stream_child_turn = _boom
    harness.run()
    parent = db.get(ChatSession, PARENT_ID)
    db.refresh(parent)
    assert ACTIVE_DISPATCH_KEY not in (parent.context_state_json or {})


# ---------------------------------------------------------------- 取消传播到子 turn


def test_propagate_cancel_touches_child_turn(db) -> None:
    """父会话取消 → 子 turn 也要被 cancel_chat_turn（真实链路的关键一跳）。"""
    from app.api.chat import _propagate_dispatch_cancel

    parent = ChatSession(
        id=PARENT_ID,
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={
            ACTIVE_DISPATCH_KEY: {
                "child_session_id": "dsp_abc",
                "child_turn_id": "turn_1:agent_hr",
            }
        },
    )
    db.add(parent)
    db.commit()

    cancelled: list[tuple[str, str]] = []

    def _fake_cancel(session_id: str, turn_id: str) -> None:
        cancelled.append((session_id, turn_id))

    import app.api.chat as chat_module

    original = chat_module.cancel_chat_turn
    chat_module.cancel_chat_turn = _fake_cancel
    try:
        _propagate_dispatch_cancel(db, TENANT, parent)
    finally:
        chat_module.cancel_chat_turn = original

    assert cancelled == [("dsp_abc", "turn_1:agent_hr")]
    db.refresh(parent)
    metadata = parent.context_state_json or {}
    assert metadata[DISPATCH_CANCEL_KEY] is True
    # 标记留着让 runner 自己清；active_dispatch 此刻还不清（runner 会在 finally 清）
    assert ACTIVE_DISPATCH_KEY in metadata


def test_propagate_cancel_marks_router_window_without_active_dispatch(db) -> None:
    """还没有子 turn（正在路由）时：不打子 turn，但必须置标记。"""
    from app.api.chat import _propagate_dispatch_cancel

    parent = ChatSession(
        id=PARENT_ID,
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
    )
    db.add(parent)
    db.commit()

    cancelled: list[tuple[str, str]] = []
    import app.api.chat as chat_module

    original = chat_module.cancel_chat_turn
    chat_module.cancel_chat_turn = lambda *args: cancelled.append(args)
    try:
        _propagate_dispatch_cancel(db, TENANT, parent)
    finally:
        chat_module.cancel_chat_turn = original

    assert cancelled == []
    db.refresh(parent)
    assert (parent.context_state_json or {}).get(DISPATCH_CANCEL_KEY) is True


def test_propagate_cancel_skips_plain_sessions(db) -> None:
    """普通会话没有路由窗口：不该往它的 context_state_json 里留调度标记。"""
    from app.api.chat import _propagate_dispatch_cancel

    plain = ChatSession(
        id="session_plain",
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=MEMBER_A,
    )
    db.add(plain)
    db.commit()

    cancelled: list[tuple[str, str]] = []
    import app.api.chat as chat_module

    original = chat_module.cancel_chat_turn
    chat_module.cancel_chat_turn = lambda *args: cancelled.append(args)
    try:
        _propagate_dispatch_cancel(db, TENANT, plain)
    finally:
        chat_module.cancel_chat_turn = original

    assert cancelled == []
    db.refresh(plain)
    assert DISPATCH_CANCEL_KEY not in (plain.context_state_json or {})


def test_propagate_cancel_survives_child_cancel_error(db) -> None:
    """子 turn 已经结束了（取消打在空气上）不该让父会话取消失败。"""
    from app.api.chat import _propagate_dispatch_cancel

    parent = ChatSession(
        id=PARENT_ID,
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={
            ACTIVE_DISPATCH_KEY: {"child_session_id": "dsp_abc", "child_turn_id": "t"}
        },
    )
    db.add(parent)
    db.commit()

    import app.api.chat as chat_module

    original = chat_module.cancel_chat_turn

    def _boom(*args, **kwargs):
        raise RuntimeError("没有这个 turn")

    chat_module.cancel_chat_turn = _boom
    try:
        _propagate_dispatch_cancel(db, TENANT, parent)
    finally:
        chat_module.cancel_chat_turn = original

    db.refresh(parent)
    assert (parent.context_state_json or {})[DISPATCH_CANCEL_KEY] is True