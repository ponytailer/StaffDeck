"""I4（qa-optimization-plan 第一批）：task_requirement 模型可见投影去重。"""

from __future__ import annotations

from app.core.engine.step_loop import _slim_requirement_payload


def test_completion_criteria_collapsed_to_single_line() -> None:
    payload = {
        "goal": "帮用户查询订单状态",
        "requirements": ["调用订单工具", "引用订单号"],
        "completion_criteria": [
            "已给出订单当前状态",
            "已说明预计送达时间",
            "已给出订单当前状态",
        ],
    }
    slimmed = _slim_requirement_payload(payload)
    assert slimmed["completion_criteria"] == (
        "已给出订单当前状态；已说明预计送达时间"
    )
    # 完整版模型不受影响（调用方继续读 Pydantic 模型）
    assert isinstance(payload["completion_criteria"], list)


def test_requirements_deduped_against_goal_and_self() -> None:
    payload = {
        "goal": "帮用户查询订单 20260101 的状态",
        "requirements": [
            "帮用户查询订单 20260101 的状态",  # 与 goal 重复
            "帮用户查询订单 20260101 的状态",  # 互相重复
            "引用最新物流信息",  # 保留
            "  引用最新物流信息  ",  # 空白差异重复
            "",
        ],
    }
    slimmed = _slim_requirement_payload(payload)
    assert slimmed["requirements"] == ["引用最新物流信息"]


def test_empty_fields_untouched() -> None:
    payload = {"goal": "问候", "requirements": [], "completion_criteria": []}
    slimmed = _slim_requirement_payload(payload)
    assert slimmed["requirements"] == []
    assert "completion_criteria" not in slimmed or slimmed["completion_criteria"] == []
    assert slimmed["goal"] == "问候"
