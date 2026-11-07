"""上下文职责切分：父会话只留**转接记录**，答案正文只用于展示。

这是整套设计里最容易在后续迭代中被破坏的一条：只要有人图省事把 assistant
正文塞进 ``dispatch_log``，下一轮路由的 prompt 就会随对话长度线性膨胀，
而且会把 A 员工的业务答案喂给 B 员工。这里的断言就是防线。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from dispatch_fixtures import (  # noqa: E402
    DISPATCHER_ID,
    MEMBER_A,
    MEMBER_B,
    OWNER_ID,
    TENANT,
    make_dispatcher,
    make_model_config,
    memory_session,
    seed_tenant,
    seed_two_members,
)

from app.db.models import AgentProfile, ChatSession  # noqa: E402
from app.dispatch.service import (  # noqa: E402
    DISPATCH_LOG_KEY,
    DISPATCH_LOG_LIMIT,
    append_dispatch_log,
    build_child_context,
    build_route_context,
    last_routed_member_id,
    read_dispatch_log,
)


@pytest.fixture()
def db():
    session = memory_session()
    seed_tenant(session)
    seed_two_members(session)
    session.add(make_dispatcher())
    session.add(make_model_config("model_dispatch", user_id=OWNER_ID))
    session.commit()
    yield session
    session.close()


def _parent(db, *, extra: dict | None = None) -> ChatSession:
    parent = ChatSession(
        id="session_parent",
        tenant_id=TENANT,
        user_id=OWNER_ID,
        agent_id=DISPATCHER_ID,
        context_state_json=dict(extra or {}),
    )
    db.add(parent)
    db.commit()
    return parent


def _entry(index: int, member_id: str = MEMBER_A, name: str = "入职小助手", query: str | None = None):
    return {
        "turn_id": f"msg_{index}",
        "member_agent_id": member_id,
        "member_name": name,
        "intent": f"意图{index}",
        "user_query": query or f"问题{index}",
        "confidence": 0.9,
    }


# ---------------------------------------------------------------- 日志读写


def test_append_and_read_roundtrip(db) -> None:
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1))
    db.commit()
    log = read_dispatch_log(parent)
    assert len(log) == 1
    assert log[0]["member_agent_id"] == MEMBER_A
    assert log[0]["user_query"] == "问题1"


def test_log_is_truncated_to_limit(db) -> None:
    parent = _parent(db)
    for index in range(DISPATCH_LOG_LIMIT + 5):
        append_dispatch_log(db, parent, _entry(index))
    db.commit()
    log = read_dispatch_log(parent)
    assert len(log) == DISPATCH_LOG_LIMIT
    # 保留的是**最近**的 N 条（滑窗），不是最早的 N 条
    assert log[-1]["user_query"] == f"问题{DISPATCH_LOG_LIMIT + 4}"


def test_log_keeps_only_whitelisted_fields(db) -> None:
    """落库时字段是白名单：多塞的键（尤其是答案正文）一律丢掉。"""
    parent = _parent(db)
    append_dispatch_log(
        db,
        parent,
        {
            **_entry(1),
            "answer": "这是员工 A 的完整业务答案，绝不该进转接记录",
            "reply": "同上",
            "prompt": "内部 prompt",
        },
    )
    db.commit()
    log = read_dispatch_log(parent)
    assert set(log[0]) == {
        "turn_id",
        "member_agent_id",
        "member_name",
        "intent",
        "user_query",
        "confidence",
    }
    assert "完整业务答案" not in str(log)


def test_read_log_tolerates_dirty_data(db) -> None:
    """手改库把 dispatch_log 写成字符串/混入非 dict，不许炸。"""
    parent = _parent(db, extra={DISPATCH_LOG_KEY: "not-a-list"})
    assert read_dispatch_log(parent) == []

    parent.context_state_json = {DISPATCH_LOG_KEY: [{"member_name": "x"}, "junk", 42]}
    db.add(parent)
    db.commit()
    log = read_dispatch_log(parent)
    assert log == [{"member_name": "x"}]


# ---------------------------------------------------------------- 路由上下文


def test_route_context_contains_only_routing_facts(db) -> None:
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1, query="我要办入职"))
    append_dispatch_log(db, parent, _entry(2, MEMBER_B, "IT 支持", "VPN 连不上"))
    db.commit()

    context = build_route_context(parent)
    assert "最近转接记录" in context
    assert "我要办入职" in context
    assert "入职小助手" in context
    assert "VPN 连不上" in context
    assert "IT 支持" in context
    # 意图带在括号里，给路由模型一个「上一轮聊什么」的短标签
    assert "（意图1）" in context


def test_route_context_is_empty_without_log(db) -> None:
    parent = _parent(db)
    assert build_route_context(parent) == ""


def test_route_context_never_reads_assistant_messages(db) -> None:
    """关键回归防线：即使父会话里堆满 assistant 长文，路由上下文也不能变。

    实现里 ``build_route_context`` 只签名收 ChatSession，不查 Message 表；
    这里用「父会话挂满消息但 context_state_json 为空」来固化这个约束。
    """
    from app.db.models import Message

    parent = _parent(db)
    for index in range(5):
        db.add(
            Message(
                tenant_id=TENANT,
                session_id=parent.id,
                role="assistant",
                content=f"第 {index} 段很长的业务答案" * 40,
                metadata_json={"dispatch": {"member_name": "入职小助手"}},
            )
        )
    db.commit()

    assert build_route_context(parent) == ""
    assert last_routed_member_id(parent) is None


def test_last_routed_member_id_is_most_recent(db) -> None:
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1, MEMBER_A))
    append_dispatch_log(db, parent, _entry(2, MEMBER_B, "IT 支持"))
    db.commit()
    assert last_routed_member_id(parent) == MEMBER_B


def test_last_routed_member_id_skips_empty_rows(db) -> None:
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1, MEMBER_A))
    append_dispatch_log(
        db,
        parent,
        {"turn_id": "x", "member_agent_id": "", "member_name": "", "user_query": "?"},
    )
    db.commit()
    assert last_routed_member_id(parent) == MEMBER_A


# ---------------------------------------------------------------- 子上下文


def test_child_context_is_none_for_same_member(db) -> None:
    """同一成员连续被路由：子会话历史已足够，重复灌摘要只会污染上下文。"""
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1))
    db.commit()
    assert build_child_context(parent, member_name="入职小助手", same_member=True) is None


def test_child_context_for_new_member_has_only_forwarding_facts(db) -> None:
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1, MEMBER_A, "入职小助手", "我要办入职"))
    append_dispatch_log(db, parent, _entry(2, MEMBER_B, "IT 支持", "VPN 连不上"))
    db.commit()

    context = build_child_context(parent, member_name="财务报销", same_member=False)
    assert context is not None
    assert "转接上下文" in context
    assert "我要办入职" in context
    assert "VPN 连不上" in context
    assert "财务报销" in context
    # 只给「问过什么、转给谁」，不给任何答案
    assert "答案" not in context.replace("可以", "")


def test_child_context_empty_log_returns_none(db) -> None:
    parent = _parent(db)
    assert build_child_context(parent, member_name="IT 支持", same_member=False) is None


def test_child_context_skips_rows_without_query(db) -> None:
    parent = _parent(db)
    append_dispatch_log(
        db,
        parent,
        {"turn_id": "x", "member_agent_id": MEMBER_A, "member_name": "入职小助手", "user_query": ""},
    )
    db.commit()
    assert build_child_context(parent, member_name="IT 支持", same_member=False) is None


def test_parent_state_isolated_from_member_qa(db) -> None:
    """父会话的 context_state_json 里除转接记录外不该被写进业务内容。"""
    parent = _parent(db)
    append_dispatch_log(db, parent, _entry(1))
    db.commit()
    db.refresh(parent)
    keys = set(parent.context_state_json or {})
    assert keys == {DISPATCH_LOG_KEY}