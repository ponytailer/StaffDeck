"""路由决策的收敛性：任何模型输出都必须落到 route / clarify / reply 三态之一。

最要紧的一条是**不许 500**：路由是每轮对话的必经环节，路由模型抽风、
返回非法 JSON、给出不在名单里的成员，都要被兜成 fallback 而不是把整轮打挂。
另外两条是成本：无成员与单成员走短路，零 LLM。
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
    MEMBER_C,
    OWNER_ID,
    TENANT,
    make_dispatcher,
    make_model_config,
    memory_session,
    member_input,
    resolved_model,
    seed_tenant,
    seed_two_members,
)

from app.api import dispatchers as dispatchers_api  # noqa: E402
from app.db.models import AgentProfile, ChatSession, DispatcherMember  # noqa: E402
from app.dispatch import router as router_module  # noqa: E402
from app.dispatch.router import DispatcherRouter  # noqa: E402
from app.dispatch.schema import DispatchConfig  # noqa: E402
from app.llm.client import LLMError  # noqa: E402


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    session.add(make_dispatcher(config=DispatchConfig(fallback_mode="clarify")))
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    session.commit()
    yield session
    session.close()


def _members(db):
    """手工组装 (成员行, 员工) 列表，绕开 DB 排序细节，专注路由逻辑。"""
    dispatcher_id = DISPATCHER_ID
    db.add(
        DispatcherMember(
            tenant_id=TENANT,
            dispatcher_agent_id=dispatcher_id,
            member_agent_id=MEMBER_A,
            routing_rule="入职、工牌、门禁、劳动合同",
            sort_order=0,
        )
    )
    db.add(
        DispatcherMember(
            tenant_id=TENANT,
            dispatcher_agent_id=dispatcher_id,
            member_agent_id=MEMBER_B,
            routing_rule="电脑、网络、VPN、账号权限；报销不要转给他",
            sort_order=1,
        )
    )
    db.commit()
    rows = db.exec(
        select(DispatcherMember).where(
            DispatcherMember.dispatcher_agent_id == dispatcher_id
        )
    ).all()
    return [
        (row, db.get(AgentProfile, row.member_agent_id)) for row in sorted(rows, key=lambda r: r.sort_order)
    ]


class _NullLLM:
    """不被允许调用的 LLM：一旦被实例化，测试就该失败。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("fast path 不该调 LLM")


def _patch_llm(monkeypatch, response: Any, *, raises: Exception | None = None) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class _Fake:
        def __init__(self, model_config: Any) -> None:
            self.model_config = model_config

        def generate_json(self, system_prompt: str, user_payload: dict[str, Any], **_: Any) -> Any:
            calls.append({"system": system_prompt, "payload": user_payload})
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr("app.llm.client.LLMClient", _Fake)
    return calls


def _decide(db, members, message, config=None, model=True, monkeypatch=None):
    return DispatcherRouter().decide(
        db=db,
        dispatcher=db.get(AgentProfile, DISPATCHER_ID),
        members=members,
        message=message,
        config=config or DispatchConfig(),
        parent_session=None,
        model_config=resolved_model("model_dispatch") if model else None,
    )


# ---------------------------------------------------------------- 短路路径


def test_no_members_falls_back_without_llm(db, monkeypatch) -> None:
    monkeypatch.setattr("app.llm.client.LLMClient", _NullLLM)
    decision = _decide(db, [], "我要办入职")
    assert decision.action == "reply"
    assert decision.fast_path is True
    assert decision.reply
    assert decision.member_agent_id is None


def test_single_member_short_circuits_without_llm(db, monkeypatch) -> None:
    monkeypatch.setattr("app.llm.client.LLMClient", _NullLLM)
    members = _members(db)[:1]
    decision = _decide(db, members, "我要办入职")
    assert decision.action == "route"
    assert decision.member_agent_id == MEMBER_A
    assert decision.confidence == 1.0
    assert decision.fast_path is True
    # 单成员时 query 原样透传，不做改写
    assert decision.query == "我要办入职"


def test_no_model_still_returns_fallback_not_exception(db) -> None:
    """模型解析失败（没配模型）时仍然要给出可用的话术，不能抛。"""
    decision = _decide(db, _members(db), "随便问", model=False)
    assert decision.action in {"clarify", "reply"}
    assert decision.reply


# ---------------------------------------------------------------- 正常路由


def test_routes_to_selected_member(db, monkeypatch) -> None:
    calls = _patch_llm(
        monkeypatch,
        {
            "action": "route",
            "member_agent_id": MEMBER_A,
            "confidence": 0.93,
            "intent": "办理入职",
            "query": "我想办理入职手续，需要准备什么材料？",
            "reason": "问题涉及入职流程",
        },
    )
    decision = _decide(db, _members(db), "我要办入职")
    assert decision.action == "route"
    assert decision.member_agent_id == MEMBER_A
    assert decision.confidence == pytest.approx(0.93)
    assert decision.intent == "办理入职"
    assert decision.query == "我想办理入职手续，需要准备什么材料？"
    # 路由 prompt 必须把成员规则原样带过去，否则准确率无从谈起
    payload = calls[0]["payload"]
    # stage_data 是展开到顶层的（stage_protocol 的约定），不是嵌套在 stage_data 键下
    rules = {item["agent_id"]: item["routing_rule"] for item in payload["members"]}
    assert "入职" in rules[MEMBER_A]
    assert "报销不要转给他" in rules[MEMBER_B]


def test_reply_action_passes_through(db, monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        {"action": "reply", "reply": "你好，我可以帮你转接同事。", "confidence": 0.9},
    )
    decision = _decide(db, _members(db), "你是谁")
    assert decision.action == "reply"
    assert decision.reply == "你好，我可以帮你转接同事。"


def test_reply_action_with_empty_text_falls_back(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "reply", "reply": "   "})
    decision = _decide(db, _members(db), "你是谁")
    assert decision.action == "reply"
    assert decision.reply  # 不许返回空字符串给用户


# ---------------------------------------------------------------- 非法输出收敛


def test_unknown_member_falls_back(db, monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        {"action": "route", "member_agent_id": "agent_not_in_list", "confidence": 0.99},
    )
    decision = _decide(db, _members(db), "随便问")
    assert decision.action == "clarify"
    assert decision.member_agent_id is None


def test_low_confidence_falls_back(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": MEMBER_A, "confidence": 0.2})
    decision = _decide(db, _members(db), "随便问", config=DispatchConfig(min_confidence=0.6))
    assert decision.action == "clarify"


def test_confidence_threshold_is_inclusive(db, monkeypatch) -> None:
    """刚好等于阈值算通过（否则 0.5 的配置会因为浮点边界随机失败）。"""
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": MEMBER_A, "confidence": 0.5})
    decision = _decide(db, _members(db), "办入职", config=DispatchConfig(min_confidence=0.5))
    assert decision.action == "route"


def test_non_json_output_falls_back(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, "这不是 JSON，是模型在自言自语")
    decision = _decide(db, _members(db), "随便问")
    assert decision.action == "clarify"
    assert decision.reply


def test_unknown_action_falls_back(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "delegate_to_human", "member_agent_id": MEMBER_A})
    decision = _decide(db, _members(db), "随便问")
    assert decision.action == "clarify"


def test_route_with_empty_query_uses_original_message(db, monkeypatch) -> None:
    """模型只给了成员没给改写文 → 用原句，不能让子员工收到空消息。"""
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": MEMBER_A, "confidence": 0.9})
    decision = _decide(db, _members(db), "我要办入职")
    assert decision.query == "我要办入职"


def test_llm_error_falls_back_without_raising(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, None, raises=LLMError("upstream 500"))
    decision = _decide(db, _members(db), "我要办入职")
    assert decision.action == "clarify"
    assert "路由模型调用失败" in (decision.reason or "")


def test_unexpected_exception_falls_back_without_raising(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, None, raises=RuntimeError("boom"))
    decision = _decide(db, _members(db), "我要办入职")
    assert decision.action == "clarify"


def test_broken_confidence_is_tolerated(db, monkeypatch) -> None:
    _patch_llm(
        monkeypatch,
        {"action": "route", "member_agent_id": MEMBER_A, "confidence": "很高"},
    )
    decision = _decide(db, _members(db), "办入职")
    assert decision.action == "clarify"  # 置信度解析失败按 0 处理


# ---------------------------------------------------------------- 三种兜底模式


def test_fallback_mode_reply_uses_configured_text(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": "nope", "confidence": 0.9})
    config = DispatchConfig(fallback_mode="reply", fallback_reply="请联系前台 8000。")
    decision = _decide(db, _members(db), "随便问", config=config)
    assert decision.action == "reply"
    assert decision.reply == "请联系前台 8000。"


def test_fallback_mode_default_member_routes(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": "nope", "confidence": 0.9})
    config = DispatchConfig(fallback_mode="default_member", default_member_agent_id=MEMBER_B)
    decision = _decide(db, _members(db), "随便问", config=config)
    assert decision.action == "route"
    assert decision.member_agent_id == MEMBER_B
    assert decision.confidence == 0.0


def test_fallback_default_member_missing_degrades_to_clarify(db, monkeypatch) -> None:
    """配了 default_member 但那个人已经不在名单里 → 退回 clarify，不要静默丢弃。"""
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": "nope", "confidence": 0.9})
    config = DispatchConfig(fallback_mode="default_member", default_member_agent_id=MEMBER_C)
    decision = _decide(db, _members(db), "随便问", config=config)
    assert decision.action == "clarify"


def test_fallback_clarify_lists_candidates(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "clarify"})
    decision = _decide(db, _members(db), "随便问")
    assert decision.action == "clarify"
    assert "入职小助手" in decision.reply
    assert "IT 支持" in decision.reply


def test_fallback_clarify_can_omit_candidates(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "clarify"})
    decision = _decide(db, _members(db), "随便问", config=DispatchConfig(offer_candidates=False))
    assert "入职小助手" not in decision.reply


# ---------------------------------------------------------------- payload 契约


def test_decision_payload_is_json_safe_and_leak_free(db, monkeypatch) -> None:
    _patch_llm(monkeypatch, {"action": "route", "member_agent_id": MEMBER_A, "confidence": 0.9})
    decision = _decide(db, _members(db), "我要办入职")
    payload = router_module.decision_payload(decision)
    assert set(payload) >= {"action", "member_agent_id", "confidence", "intent", "query", "reason"}
    # 不许把 prompt 或成员私有规则塞进事件
    assert "system" not in payload
    assert "routing_rule" not in payload


def test_route_context_is_sent_to_llm(db, monkeypatch) -> None:
    """父会话的转接记录必须进 payload，否则「接着上面那件事」这类指代无法解析。"""
    calls = _patch_llm(monkeypatch, {"action": "clarify"})
    parent = ChatSession(
        id="session_parent",
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json={
            "dispatch_log": [
                {
                    "turn_id": "m1",
                    "member_agent_id": MEMBER_A,
                    "member_name": "入职小助手",
                    "intent": "办理入职",
                    "user_query": "我要办入职",
                    "confidence": 0.9,
                }
            ]
        },
    )
    db.add(parent)
    db.commit()
    DispatcherRouter().decide(
        db=db,
        dispatcher=db.get(AgentProfile, DISPATCHER_ID),
        members=_members(db),
        message="那材料呢",
        config=DispatchConfig(),
        parent_session=parent,
        model_config=resolved_model("model_dispatch"),
    )
    payload = calls[0]["payload"]
    assert "我要办入职" in payload["route_context"]
    assert payload["last_routed_member_id"] == MEMBER_A


# ---------------------------------------------------------------- 模型必填


def test_ensure_model_rejects_missing_and_foreign(db) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as missing:
        dispatchers_api._ensure_model(db, TENANT, None)
    assert missing.value.status_code == 400
    assert "must configure a model" in str(missing.value.detail)

    with pytest.raises(HTTPException) as foreign:
        dispatchers_api._ensure_model(db, TENANT, "model_missing")
    assert foreign.value.status_code == 400