"""I6 增量（qa-optimization-plan 第二批）：planner 会话续跑短路。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core.plan import planner as planner_module
from app.core.plan.planner import _followup_shortcircuit_plan
from app.db.models import Skill

HISTORY = {"messages": [{"role": "user", "content": "上一轮的问题"}]}


def _skill(trigger: list[str] | None = None) -> Skill:
    return Skill(
        id="skill_row_1",
        tenant_id="tenant_demo",
        skill_id="sop_refund",
        name="退款",
        content_json={"trigger_intents": trigger or []},
    )


def _session(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "id": "session_1",
        "tenant_id": "tenant_demo",
        "active_skill_id": None,
        "active_step_id": None,
        "pending_tasks_json": None,
        "awaiting_input_json": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_short_followup_short_circuits() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "那住客呢",
        [_skill()],
        HISTORY,
        None,
        "normal",
        None,
    )
    assert plan is not None
    assert plan.decision == "answer_only"
    assert plan.task_frames[0].kind == "conversation"
    assert plan.task_frames[0].execution_mode == "standard"
    assert plan.task_frames[0].source_message == "那住客呢"


def test_long_message_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "帮我查一下昨天入住的住客的订单状态，然后把发票重新开一张",
        [_skill()],
        HISTORY,
        None,
        "normal",
        None,
    )
    assert plan is None


def test_trigger_intent_hit_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "帮我退款",
        [_skill(trigger=["退款", "退货"])],
        HISTORY,
        None,
        "normal",
        None,
    )
    assert plan is None


def test_active_sop_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(active_skill_id="sop_refund", active_step_id="step_1"),
        "那住客呢",
        [_skill()],
        HISTORY,
        None,
        "normal",
        None,
    )
    assert plan is None


def test_pending_frame_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "那住客呢",
        [_skill()],
        HISTORY,
        [{"task_id": "task_1", "status": "queued"}],
        "normal",
        None,
    )
    assert plan is None


def test_first_message_without_history_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "那住客呢",
        [_skill()],
        None,
        None,
        "normal",
        None,
    )
    assert plan is None


def test_non_normal_mode_falls_back_to_llm() -> None:
    plan = _followup_shortcircuit_plan(
        _session(),
        "那住客呢",
        [_skill()],
        HISTORY,
        None,
        "scheduled_task",
        None,
    )
    assert plan is None


def test_flag_disabled_falls_back_to_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import get_settings

    monkeypatch.setattr(
        get_settings(), "planner_followup_shortcircuit_enabled", False
    )
    plan = _followup_shortcircuit_plan(
        _session(),
        "那住客呢",
        [_skill()],
        HISTORY,
        None,
        "normal",
        None,
    )
    assert plan is None
