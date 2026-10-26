"""I5（qa-optimization-plan 第二批）：memory.capture 无信息轮门控。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.engine.facade import _memory_capture_skip_reason
from app.session.session_schema import StepAgentResult


@pytest.fixture()
def informative_result() -> StepAgentResult:
    return StepAgentResult(slot_updates={"user_name": "hm"})


def test_smalltalk_skipped() -> None:
    assert (
        _memory_capture_skip_reason("你好呀", StepAgentResult())
        == "smalltalk"
    )


def test_uninformative_turn_skipped() -> None:
    """纯问答轮：无槽位写入、无 handoff、无结构化结果 → 跳过。"""

    result = StepAgentResult(
        reply="已为你查到相关知识。",
        knowledge_results=[{"chunk_id": "c1"}],
    )
    assert (
        _memory_capture_skip_reason("报销标准是多少", result)
        == "uninformative_turn"
    )


def test_explicit_memory_request_never_skipped() -> None:
    assert (
        _memory_capture_skip_reason("记住我喜欢靠窗的座位", StepAgentResult())
        is None
    )
    assert (
        _memory_capture_skip_reason("别忘了我是金牌会员", StepAgentResult())
        is None
    )


def test_state_write_not_skipped(informative_result: StepAgentResult) -> None:
    assert _memory_capture_skip_reason("我叫hm", informative_result) is None


def test_handoff_not_skipped() -> None:
    result = StepAgentResult(handoff=True)
    assert _memory_capture_skip_reason("转人工", result) is None


def test_structured_result_not_skipped() -> None:
    result = StepAgentResult(structured_result={"order_status": "shipped"})
    assert _memory_capture_skip_reason("查下订单", result) is None


def test_flag_disabled_always_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.engine.facade.get_settings",
        lambda: SimpleNamespace(memory_capture_skip_uninformative_enabled=False),
    )
    assert _memory_capture_skip_reason("你好呀", StepAgentResult()) is None
