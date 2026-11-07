"""一轮调度委派的执行链路：子会话、模型归属、用量、事件镜像。

四条硬约束（方案 §2/§3/§4.5）：
1. 子会话独立且 ``channel == "dispatch_child"``（不出现在侧边栏）；
   同一父会话 × 同一成员 → 子会话 id 稳定，历史接得上。
2. 模型：成员自己配了就用成员的（谁配谁付费），没配才用调度入口的；
   **两种情况都不许走免费模型链路**。
3. 用量：成员 ``usage_count`` 不增（子会话是预创建的），调度入口承担计数。
4. 事件：只镜像流式增量，不镜像 session_created / user_message_received / complete。
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
    make_agent,
    make_dispatcher,
    make_model_config,
    memory_session,
    member_input,
    resolved_model,
    seed_tenant,
    seed_two_members,
)

from app.db.models import AgentEvent, AgentProfile, ChatSession, Message, ModelConfig  # noqa: E402
from app.dispatch.schema import DispatchRouteDecision  # noqa: E402
from app.dispatch.service import (  # noqa: E402
    ACTIVE_DISPATCH_KEY,
    DISPATCH_CANCEL_KEY,
    DispatchValidationError,
    build_child_context,
    child_session_id,
    ensure_child_session,
    read_dispatch_log,
    replace_members,
    resolve_dispatch_member_model,
    resolve_dispatcher_model,
)
from app.dispatch.stream import MIRROR_EVENTS, DispatchTurnRunner  # noqa: E402
from app.session.origin import DISPATCH_CHILD_CHANNEL, HIDDEN_FROM_CHAT_LIST_CHANNELS  # noqa: E402
from app.session.session_schema import ChatTurnRequest  # noqa: E402


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    dispatcher = make_dispatcher()
    session.add(dispatcher)
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    session.commit()
    # 调度入口必须自己绑一条模型（API 层强校验，直接从库里造就得手工补）
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


def _request(**overrides) -> ChatTurnRequest:
    payload: dict[str, Any] = {
        "tenant_id": TENANT,
        "session_id": "session_parent",
        "agent_id": DISPATCHER_ID,
        "user_id": OWNER_ID,
        "client_turn_id": "turn_1",
        "message": "我要办入职",
        "channel": "web",
    }
    payload.update(overrides)
    return ChatTurnRequest(**payload)


def _parent(db) -> ChatSession:
    existing = db.get(ChatSession, "session_parent")
    if existing is not None:
        return existing
    parent = ChatSession(
        id="session_parent",
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
    )
    db.add(parent)
    db.commit()
    return parent


def _runner(decision: DispatchRouteDecision, *, reply: str = "好的，需要准备身份证和学历证明。", fake_loop=None):
    events: list[tuple[str, dict[str, Any]]] = []
    captured: list[ChatTurnRequest] = []

    def emit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    runner = DispatchTurnRunner(emit=emit, session_factory=lambda: db_holder["db"])

    class _Router:
        def decide(self, **kwargs):
            router_calls.append(kwargs)
            return decision

    router_calls: list[dict[str, Any]] = []
    runner._router = _Router()

    if fake_loop is not None:
        runner._stream_child_turn = fake_loop(captured, reply)
    return runner, events, captured, router_calls


db_holder: dict[str, Any] = {}


@pytest.fixture(autouse=True)
def _bind_db_holder(request):
    """把当前测试的 db 塞给 runner 的 session_factory（子会话用同一个内存库）。"""
    yield


def _run(db, runner, request=None, parent_turn_id: str = ""):
    db_holder["db"] = db
    parent = _parent(db)
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    req = request or _request()
    return runner.run(db, req, parent, dispatcher, parent_turn_id=parent_turn_id), parent


def _fake_loop(captured, reply):
    # 签名必须跟 DispatchTurnRunner._stream_child_turn 完全一致：真实实现新增了
    # parent_turn_id / parent_client_turn_id（用于把子 turn 的增量改写成父轮）。
    def _stream(
        child_request,
        *,
        parent_session_id,
        parent_turn_id="",
        parent_client_turn_id=None,
    ):
        captured.append(child_request)
        return reply

    return _stream


# ---------------------------------------------------------------- 事件镜像


def test_mirror_event_whitelist() -> None:
    """session_created / user_message_received / complete 由父会话自己发。"""
    assert "stream_delta" in MIRROR_EVENTS
    assert "stream_status" in MIRROR_EVENTS
    assert "session_created" not in MIRROR_EVENTS
    assert "user_message_received" not in MIRROR_EVENTS
    assert "complete" not in MIRROR_EVENTS


def test_child_channel_is_hidden_from_sidebar() -> None:
    assert DISPATCH_CHILD_CHANNEL in HIDDEN_FROM_CHAT_LIST_CHANNELS


# ---------------------------------------------------------------- 路由分支


def test_route_writes_parent_snapshot_and_log(db) -> None:
    runner, events, captured, _ = _runner(
        DispatchRouteDecision(
            action="route",
            member_agent_id=MEMBER_A,
            confidence=0.92,
            intent="办理入职",
            query="我想办入职，需要什么材料",
        ),
        fake_loop=_fake_loop,
    )
    result, parent = _run(db, runner, parent_turn_id="msg_turn_route")

    # 子请求：消息用改写后的 query，不是原句
    assert len(captured) == 1
    assert captured[0].message == "我想办入职，需要什么材料"
    assert captured[0].agent_id == MEMBER_A

    # 父会话落两条 assistant：① 路由的「已转交给 X」通知 ② 成员答案快照。
    # 通知靠 metadata.dispatch_notice 识别，前端据此渲染成独立的「路由」气泡；
    # 它**不是**答案，所以答案那条必须带 metadata.dispatch 的成员信息。
    replies = db.exec(
        select(Message).where(
            Message.session_id == parent.id, Message.role == "assistant"
        ).order_by(Message.created_at)
    ).all()
    assert len(replies) == 2
    notice, answer = replies
    assert notice.metadata_json["dispatch_notice"]["member_agent_id"] == MEMBER_A
    assert notice.metadata_json["dispatch_notice"]["member_name"] == "入职小助手"
    assert "dispatch" not in notice.metadata_json
    assert notice.content == "已转交给 入职小助手"
    # 两条都要带 client_turn_id：前端刷新后靠它把本地 turn 和服务端轮次认成一轮，
    # 缺了会触发 loadTraces 僵尸复活 → 消息永远卡「排队中」。
    assert notice.metadata_json["client_turn_id"] == "turn_1"
    # turn_id 是父会话这一轮的用户消息 id（run 收到 parent_turn_id），非 client turn id
    assert notice.metadata_json["turn_id"] == "msg_turn_route"
    assert notice.metadata_json["user_message_id"] == "msg_turn_route"

    assert answer.content == "好的，需要准备身份证和学历证明。"
    assert answer.metadata_json["dispatch"]["member_agent_id"] == MEMBER_A
    assert answer.metadata_json["dispatch"]["intent"] == "办理入职"
    assert answer.metadata_json["client_turn_id"] == "turn_1"

    # 转接记录只有一条，且不含答案正文
    log = read_dispatch_log(parent)
    assert len(log) == 1
    assert log[0]["user_query"] == "我要办入职"
    assert "身份证" not in str(log)

    assert result["reply"] == "好的，需要准备身份证和学历证明。"
    assert result["dispatch"]["member_name"] == "入职小助手"
    # 顺序：决策 → 已转交（状态条）→ 转接通知 → 子 turn 的增量
    names = [name for name, _ in events]
    assert names[0] == "dispatch_decision"
    assert names[1] == "stream_status"
    assert events[1][1]["phase"] == "dispatched"
    assert names[2] == "dispatch_notice"
    assert events[2][1]["member_name"] == "入职小助手"


def test_route_writes_assistant_message_created_event(db) -> None:
    """dispatch 分支必须补 ``assistant_message_created``，否则总耗时虚高。

    ``_build_turn_traces`` 只认这个事件给 trace 收尾（置 ``completed_at``）。
    dispatch 以前从不写它，那一轮的 ``completed_at`` 恒为 null，最后被
    ``fallback_time = events[-1].created_at``（**整个会话最后一条事件**的时间）
    强行补上——「成员只跑 30s，界面显示 3min」就是这么来的（实测某轮 51min）。
    """
    runner, _events, _captured, _ = _runner(
        DispatchRouteDecision(
            action="route",
            member_agent_id=MEMBER_A,
            confidence=0.92,
            intent="办理入职",
            query="我想办入职，需要什么材料",
        ),
        fake_loop=_fake_loop,
    )
    _result, parent = _run(db, runner, parent_turn_id="msg_turn_created")

    created = db.exec(
        select(AgentEvent).where(
            AgentEvent.session_id == parent.id,
            AgentEvent.event_type == "assistant_message_created",
        )
    ).all()
    assert len(created) == 1, "dispatch 答案必须有自己的 assistant_message_created"
    payload = created[0].payload_json
    # turn 归属必须指向父会话这一轮的用户消息，不能是子会话的
    assert payload["turn_id"] == "msg_turn_created"
    assert payload["user_message_id"] == "msg_turn_created"
    assert payload["client_turn_id"] == "turn_1"
    assert payload["reply"] == "好的，需要准备身份证和学历证明。"


def test_clarify_writes_assistant_message_created_event(db) -> None:
    """兜底分支（clarify/reply/error）同样要收尾，不然同样耗时虚高。"""
    runner, _events, _captured, _ = _runner(
        DispatchRouteDecision(action="clarify", reply="你想咨询哪方面的问题？"),
        fake_loop=_fake_loop,
    )
    _result, parent = _run(db, runner, parent_turn_id="msg_turn_clarify")
    created = db.exec(
        select(AgentEvent).where(
            AgentEvent.session_id == parent.id,
            AgentEvent.event_type == "assistant_message_created",
        )
    ).all()
    assert len(created) == 1
    assert created[0].payload_json["turn_id"] == "msg_turn_clarify"


def test_clarify_writes_text_without_child_turn(db) -> None:
    runner, events, captured, _ = _runner(
        DispatchRouteDecision(action="clarify", reply="你想咨询哪方面的问题？", reason="判断不出"),
        fake_loop=_fake_loop,
    )
    result, parent = _run(db, runner)
    assert captured == []
    assert result["reply"] == "你想咨询哪方面的问题？"
    reply = db.exec(
        select(Message).where(Message.session_id == parent.id, Message.role == "assistant")
    ).one()
    assert reply.metadata_json["dispatch"]["action"] == "clarify"
    # clarify 不写转接记录（没转给任何人）
    assert read_dispatch_log(parent) == []
    assert [name for name, _ in events] == ["dispatch_decision"]


def test_reply_action_recorded_as_reply(db) -> None:
    runner, _, captured, _ = _runner(
        DispatchRouteDecision(action="reply", reply="你好，我可以帮你转接。"),
        fake_loop=_fake_loop,
    )
    result, parent = _run(db, runner)
    assert captured == []
    reply = db.exec(
        select(Message).where(Message.session_id == parent.id, Message.role == "assistant")
    ).one()
    assert reply.metadata_json["dispatch"]["action"] == "reply"


def test_route_to_missing_member_degrades(db) -> None:
    """路由后成员被并发删掉 → 走兜底，不抛。"""
    runner, _, captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id="agent_ghost", confidence=0.9),
        fake_loop=_fake_loop,
    )
    result, _ = _run(db, runner)
    assert captured == []
    assert result["dispatch"]["action"] == "clarify"
    assert result["reply"]


def test_child_turn_failure_is_swallowed(db) -> None:
    def _boom_factory(captured, reply):
        def _boom(
            child_request,
            *,
            parent_session_id,
            parent_turn_id="",
            parent_client_turn_id=None,
        ):
            captured.append(child_request)
            raise RuntimeError("子 turn 炸了")

        return _boom

    runner, events, captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_boom_factory,
    )
    result, parent = _run(db, runner)
    assert "出错了" in result["reply"]
    assert result["dispatch"]["action"] == "error"
    assert any(name == "dispatch_failed" for name, _ in events)
    # 失败的轮次不留转接记录
    assert read_dispatch_log(parent) == []


def test_empty_child_reply_falls_back(db) -> None:
    runner, _, _, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
        reply="   ",
    )
    result, _ = _run(db, runner)
    assert "没有给出回复" in result["reply"]


# ---------------------------------------------------------------- event_sink 镜像


def test_stream_child_turn_mirrors_via_event_sink(db, monkeypatch) -> None:
    """子 turn 的真增量只能靠 ``event_sink`` 镜像到父会话。

    ``TurnStreamSink.delta()`` 是**直接写子会话 AgentEvent 表**的
    （``sink.delta → _record → EventLog.record``），根本不经过
    ``handle_turn_stream`` 的生成器。只转发生成器 yield 的话父会话一条真增量
    都拿不到，用户看到的就是「转交后卡三分钟、答案一次性蹦出来」（真实踩过）。
    """
    import app.dispatch.stream as stream_module

    mirrored: list[tuple[str, dict[str, Any]]] = []

    class _FakeLoop:
        def __init__(self, _db, *, event_sink=None):
            self._sink = event_sink

        def handle_turn_stream(self, _request):
            # 真实链路里这些是 sink.delta/status 触发的 event_sink 回调，
            # payload 已由 EventLog.record 补好子轮的 turn_id/client_turn_id。
            assert self._sink is not None, "必须把 event_sink 传给子 AgentLoop"
            self._sink("stream_delta", {"content": "准备", "sessionId": "child_sess"})
            self._sink("stream_delta", {"content": "身份证", "sessionId": "child_sess"})
            self._sink("session_created", {"sessionId": "child_sess"})  # 不在白名单
            self._sink("complete", {"reply": "准备身份证"})  # 不在白名单
            yield {"event": "complete", "data": {"reply": "准备身份证"}}

    monkeypatch.setattr(
        "app.core.engine.facade.AgentLoop", _FakeLoop, raising=False
    )

    runner, _events, _captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
    )
    runner._emit = lambda name, payload: mirrored.append((name, payload))

    # 直接调用真实实现（_runner 默认不注入 fake_loop）
    child_request = ChatTurnRequest(
        tenant_id=TENANT,
        agent_id=MEMBER_A,
        user_id=OWNER_ID,
        message="我想办入职",
        session_id="child_sess",
    )
    reply = runner._stream_child_turn(
        child_request,
        parent_session_id="parent_sess",
        parent_turn_id="msg_parent_turn",
        parent_client_turn_id="turn_1",
    )

    assert reply == "准备身份证"
    # 白名单外的事件不许镜像
    assert [name for name, _ in mirrored] == ["stream_delta", "stream_delta"]
    # 增量内容保留，轮次归属改写成父轮，会话改写成父会话
    assert [payload["content"] for _, payload in mirrored] == ["准备", "身份证"]
    for _name, payload in mirrored:
        assert payload["sessionId"] == "parent_sess"
        assert payload["turn_id"] == "msg_parent_turn"
        assert payload["user_message_id"] == "msg_parent_turn"
        assert payload["client_turn_id"] == "turn_1"
        assert payload["dispatch_child_session_id"] == "child_sess"
    # 兜底：模块级白名单没被改动
    assert "complete" not in stream_module.MIRROR_EVENTS


def test_mirrored_text_count_tracks_stream_text_only(db, monkeypatch) -> None:
    """``mirrored_text_count`` 只管文本事件——调用方靠它判断要不要整段回放。

    统计错（把 stream_status 也算进去）就会误判「已镜像」→ 子 turn 只发了状态、
    没有正文时也不回放，用户看到空气泡。
    """

    class _FakeLoop:
        def __init__(self, _db, *, event_sink=None):
            self._sink = event_sink

        def handle_turn_stream(self, _request):
            self._sink("stream_status", {"phase": "preparing", "text": "正在生成回复"})
            self._sink("stream_delta", {"content": "甲"})
            self._sink("stream_replace", {"content": "甲乙"})
            self._sink("knowledge_result", {"items": []})  # 非文本
            self._sink("complete", {"reply": "甲乙"})
            yield {"event": "complete", "data": {"reply": "甲乙"}}

    monkeypatch.setattr("app.core.engine.facade.AgentLoop", _FakeLoop, raising=False)

    runner, _events, _captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
    )
    runner._emit = lambda name, payload: None
    assert runner.mirrored_text_count == 0

    reply = runner._stream_child_turn(
        ChatTurnRequest(
            tenant_id=TENANT,
            agent_id=MEMBER_A,
            user_id=OWNER_ID,
            message="你好",
            session_id="child_sess",
        ),
        parent_session_id="parent_sess",
        parent_turn_id="msg_t",
    )
    assert reply == "甲乙"
    # 只有 stream_delta + stream_replace 计 2；stream_status / knowledge_result 不算
    assert runner.mirrored_text_count == 2


# ---------------------------------------------------------------- 子会话


def test_child_session_is_stable_and_hidden(db) -> None:
    parent = _parent(db)
    member_a = db.get(AgentProfile, MEMBER_A)
    member_b = db.get(AgentProfile, MEMBER_B)

    first = ensure_child_session(db, parent, member_a)
    db.commit()
    second = ensure_child_session(db, parent, member_a)
    db.commit()
    other = ensure_child_session(db, parent, member_b)
    db.commit()

    assert first.id == second.id == child_session_id(parent.id, MEMBER_A)
    assert first.id != other.id
    assert first.channel == DISPATCH_CHILD_CHANNEL
    assert first.agent_id == MEMBER_A
    assert first.context_state_json["dispatch_parent_session_id"] == parent.id


def test_child_session_does_not_bump_member_usage(db) -> None:
    parent = _parent(db)
    before = db.get(AgentProfile, MEMBER_A).usage_count
    ensure_child_session(db, parent, db.get(AgentProfile, MEMBER_A))
    db.commit()
    db.refresh(db.get(AgentProfile, MEMBER_A))
    assert db.get(AgentProfile, MEMBER_A).usage_count == before


def test_child_session_reuses_existing_row(db) -> None:
    parent = _parent(db)
    member = db.get(AgentProfile, MEMBER_A)
    created = ensure_child_session(db, parent, member)
    db.commit()
    session_id = created.id
    # 第二轮不该再插入
    again = ensure_child_session(db, parent, member)
    db.commit()
    rows = db.exec(select(ChatSession).where(ChatSession.id == session_id)).all()
    assert len(rows) == 1
    assert again.id == session_id


# ---------------------------------------------------------------- 模型归属


def test_member_with_own_model_uses_it(db) -> None:
    dispatcher_model = resolved_model("model_dispatch")
    member = db.get(AgentProfile, MEMBER_A)
    resolved = resolve_dispatch_member_model(db, TENANT, member, dispatcher_model, OWNER_ID)
    assert resolved.id == "model_hr"


def test_member_without_model_falls_back_to_dispatcher(db) -> None:
    db.add(make_agent("agent_bare", name="没配模型的同事", owner_id=OWNER_ID))
    db.commit()
    dispatcher_model = resolved_model("model_dispatch")
    member = db.get(AgentProfile, "agent_bare")
    resolved = resolve_dispatch_member_model(db, TENANT, member, dispatcher_model, OWNER_ID)
    assert resolved.id == dispatcher_model.id


def test_member_model_resolution_never_touches_free_model(db, monkeypatch) -> None:
    """「谁配谁付费」的关键补丁：解析子 turn 模型时不许调免费回落。"""

    def _forbidden(*args, **kwargs):
        raise AssertionError("resolve_turn_default_model 不该被调用")

    monkeypatch.setattr(
        "app.agents.branching.resolve_turn_default_model", _forbidden, raising=False
    )
    db.add(make_agent("agent_bare", name="没配模型的同事", owner_id=OWNER_ID))
    db.commit()
    member = db.get(AgentProfile, "agent_bare")
    resolved = resolve_dispatch_member_model(
        db, TENANT, member, resolved_model("model_dispatch"), OWNER_ID
    )
    assert resolved.id == "model_dispatch"


def test_dispatcher_model_resolution_does_not_use_free_fallback(db) -> None:
    """调度入口自己绑的模型优先；即使存在全局免费模型也不该被选中。"""
    db.add(
        make_model_config(
            "model_free", user_id=None, is_default=True, is_global_free=True
        )
    )
    db.commit()
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    resolved = resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)
    assert resolved.id == "model_dispatch"


def test_dispatcher_without_model_falls_back_to_tenant_model(db) -> None:
    """绑定的模型被删/停用时回落到租户内可用模型，而不是整轮失败。

    API 层保证「必须配置模型才能创建」，这里的回落是给「模型后来被停用」兜底。
    """
    dispatcher = make_dispatcher("agent_bare_dispatch", name="没配模型的入口")
    db.add(dispatcher)
    db.commit()
    resolved = resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)
    # 租户里只有 model_hr / model_it / model_dispatch（都不是全局免费）
    assert resolved.id in {"model_hr", "model_it", "model_dispatch"}


def test_dispatcher_without_any_model_raises(db) -> None:
    """租户里一条可用模型都没有 → 抛 DispatchValidationError，由 runner 译成可读文案。"""
    for row in db.exec(select(ModelConfig)).all():
        row.enabled = False
        db.add(row)
    db.commit()
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    with pytest.raises(DispatchValidationError):
        resolve_dispatcher_model(db, TENANT, dispatcher, OWNER_ID)


def test_child_request_carries_model_config_id(db) -> None:
    """子请求显式带 model_config_id 是关闭免费链路的关键，必须固化。"""
    runner, _, captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner)
    assert captured[0].model_config_id == "model_hr"


def test_child_request_carries_model_config_id_for_bare_member(db) -> None:
    db.add(make_agent("agent_bare", name="没配模型的同事", owner_id=OWNER_ID))
    db.commit()
    replace_members(
        db,
        db.get(AgentProfile, DISPATCHER_ID),
        [member_input(MEMBER_A), member_input("agent_bare")],
    )
    db.commit()
    runner, _, captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id="agent_bare", confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner)
    assert captured[0].model_config_id == "model_dispatch"


# ---------------------------------------------------------------- 子上下文注入


def test_first_turn_has_no_child_context(db) -> None:
    runner, _, captured, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner)
    assert captured[0].context_injection is None


def test_same_member_second_turn_has_no_child_context(db) -> None:
    """同一成员连续被路由：子会话自己的历史够用，不要再灌摘要。"""
    decision = DispatchRouteDecision(
        action="route", member_agent_id=MEMBER_A, confidence=0.9
    )
    runner, _, captured, _ = _runner(decision, fake_loop=_fake_loop)
    _run(db, runner)
    runner2, _, captured2, _ = _runner(decision, fake_loop=_fake_loop)
    _run(db, runner2, _request(client_turn_id="turn_2", message="那材料呢"))
    assert captured2[0].context_injection is None


def test_switching_member_injects_forwarding_context(db) -> None:
    runner, _, _, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner)

    runner2, _, captured2, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_B, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner2, _request(client_turn_id="turn_2", message="VPN 连不上"))
    injection = captured2[0].context_injection
    assert injection is not None
    assert "我要办入职" in injection
    assert "IT 支持" in injection
    # 不含上一轮的答案正文
    assert "身份证" not in injection


def test_build_child_context_direct(db) -> None:
    parent = _parent(db)
    parent.context_state_json = {
        "dispatch_log": [
            {
                "turn_id": "m1",
                "member_agent_id": MEMBER_A,
                "member_name": "入职小助手",
                "intent": "办理入职",
                "user_query": "我要办入职",
            }
        ]
    }
    db.add(parent)
    db.commit()
    assert build_child_context(parent, member_name="入职小助手", same_member=True) is None
    assert build_child_context(parent, member_name="IT 支持", same_member=False) is not None


# ---------------------------------------------------------------- 取消


def test_cancel_during_routing_window_skips_child_turn(db) -> None:
    """路由窗口（1~2s）里点取消：还没起子 turn，直接收尾，不白花钱。

    run() 开头会先清「上一轮遗留」的标记，所以这里必须模拟取消发生在**路由过程中**
    （从 decide() 里写标记），而不是 run() 之前。
    """
    parent = _parent(db)
    events: list[tuple[str, dict[str, Any]]] = []
    captured: list[ChatTurnRequest] = []
    db_holder["db"] = db

    class _CancellingRouter:
        def decide(self, **kwargs):
            fresh = kwargs["db"].get(ChatSession, parent.id)
            metadata = dict(fresh.context_state_json or {})
            metadata[DISPATCH_CANCEL_KEY] = True
            fresh.context_state_json = metadata
            kwargs["db"].add(fresh)
            kwargs["db"].commit()
            return DispatchRouteDecision(
                action="route", member_agent_id=MEMBER_A, confidence=0.9
            )

    runner = DispatchTurnRunner(
        emit=lambda name, payload: events.append((name, payload)),
        session_factory=lambda: db_holder["db"],
    )
    runner._router = _CancellingRouter()
    runner._stream_child_turn = _fake_loop(captured, "不该执行")

    result = runner.run(db, _request(), parent, db.get(AgentProfile, DISPATCHER_ID))
    assert result == {"cancelled": True}
    assert captured == []
    assert any(name == "stream_cancelled" for name, _ in events)
    # 取消后 active_dispatch 必须清掉，否则下一次取消会打到不存在的子 turn
    db.refresh(parent)
    assert ACTIVE_DISPATCH_KEY not in (parent.context_state_json or {})


def test_stale_cancel_flag_is_cleared_on_next_turn(db) -> None:
    """取消过一次不能把这个会话之后每一轮都取消掉。"""
    decision = DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9)
    parent = _parent(db)
    parent.context_state_json = {DISPATCH_CANCEL_KEY: True}
    db.add(parent)
    db.commit()

    runner, _, captured, _ = _runner(decision, fake_loop=_fake_loop)
    db_holder["db"] = db
    runner.run(db, _request(), parent, db.get(AgentProfile, DISPATCHER_ID))
    db.commit()
    db.refresh(parent)
    assert DISPATCH_CANCEL_KEY not in (parent.context_state_json or {})

    runner2, _, captured2, _ = _runner(decision, fake_loop=_fake_loop)
    db_holder["db"] = db
    runner2.run(db, _request(client_turn_id="turn_2"), parent, db.get(AgentProfile, DISPATCHER_ID))
    assert len(captured2) == 1


def test_active_dispatch_is_cleared_after_turn(db) -> None:
    runner, _, _, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _, parent = _run(db, runner)
    db.refresh(parent)
    assert ACTIVE_DISPATCH_KEY not in (parent.context_state_json or {})


# ---------------------------------------------------------------- 用量


def test_dispatch_does_not_bump_member_usage(db) -> None:
    """成员用量不因路由而增长（子会话是预创建的，跳过 _bump_agent_usage）。"""
    before = db.get(AgentProfile, MEMBER_A).usage_count
    runner, _, _, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    _run(db, runner)
    db.commit()
    assert db.get(AgentProfile, MEMBER_A).usage_count == before

# ---------------------------------------------------------------- turn_id（气泡去重）


def test_parent_assistant_message_carries_turn_id(db) -> None:
    """父会话的 assistant 消息必须带 turn_id，否则答案会渲染成两个气泡。

    真实踩过：dispatch 分支没有 ``assistant_message_created`` 事件，
    ``message_read`` 只能靠 ``metadata.turn_id`` 回退推 turn_id。缺了它，
    前端 ``computeMergedMessages`` 认不出「服务端消息」与「实时流式消息」是同一轮，
    两条都保留 → 用户看到同一个答案显示两遍。
    """
    runner, _, _, _ = _runner(
        DispatchRouteDecision(action="route", member_agent_id=MEMBER_A, confidence=0.9),
        fake_loop=_fake_loop,
    )
    db_holder["db"] = db
    parent = _parent(db)
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    runner.run(db, _request(), parent, dispatcher, parent_turn_id="msg_turn_1")
    db.commit()

    assistant = db.exec(
        select(Message).where(Message.session_id == parent.id, Message.role == "assistant")
    ).first()
    assert assistant is not None
    metadata = assistant.metadata_json or {}
    assert metadata.get("turn_id") == "msg_turn_1"
    assert metadata.get("user_message_id") == "msg_turn_1"


def test_fallback_text_also_carries_turn_id(db) -> None:
    """clarify / reply / error 这些纯文本兜底同样要带 turn_id。"""
    from app.dispatch.schema import DEFAULT_FALLBACK_REPLY

    runner, _, _, _ = _runner(DispatchRouteDecision(action="clarify", confidence=0.1))
    db_holder["db"] = db
    parent = _parent(db)
    dispatcher = db.get(AgentProfile, DISPATCHER_ID)
    result = runner.run(db, _request(), parent, dispatcher, parent_turn_id="msg_turn_2")
    db.commit()

    # clarify 分支走的是配置里的 fallback_reply（测试夹具用默认配置）
    assert result["reply"] == DEFAULT_FALLBACK_REPLY
    assistant = db.exec(
        select(Message).where(Message.session_id == parent.id, Message.role == "assistant")
    ).first()
    assert assistant is not None
    assert (assistant.metadata_json or {}).get("turn_id") == "msg_turn_2"
