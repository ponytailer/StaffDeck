"""SOP 确定性执行器（P1）测试。

覆盖三个确定性场景（预检索/纯流转/缺槽询问）的触发与降级边界，
以及挂载到 HarnessTaskAgent 后「纯流转节点零 LLM」的集成行为。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.harness_agent import HarnessTaskAgent
from app.core.sop_step_executor import TRACE_EVENT, plan_sop_prefill_actions
from app.core.task_request_compiler import (
    CapabilityManifest,
    TaskRequirement,
)


def _sop_requirement(**overrides: Any) -> TaskRequirement:
    base: dict[str, Any] = {
        "task_frame_id": "task-1",
        "kind": "sop",
        "goal": "完成 SOP 步骤",
        "source_user_message": "帮我申领一台电脑",
        "sop_context": {
            "skill_id": "sop-1",
            "skill_name": "IT 服务",
            "step": {
                "node_id": "n2",
                "name": "提交申请",
                "instruction": "提交申请单",
                "expected_user_info": ["device_model", "urgency"],
            },
        },
        "allowed_transitions": [{"next_node_id": "n3", "condition": ""}],
        "capability_manifest": CapabilityManifest(),
    }
    base.update(overrides)
    return TaskRequirement(**base)


def _events() -> list[tuple[str, dict[str, Any]]]:
    return []


# ---------------------------------------------------------------------------
# 场景 C：预检索
# ---------------------------------------------------------------------------


def test_prefetch_knowledge_when_required_kb_and_no_slot_gap() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_slots=[],
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "tool"
    assert action["tool_name"] == "knowledge_search"
    assert action["arguments"]["query"] == "帮我申领一台电脑"
    assert action["arguments"]["knowledge_base_ids"] == ["kb-it"]
    assert events and events[0][0] == TRACE_EVENT
    assert events[0][1]["scene"] == "prefetch_knowledge"


def test_prefetch_skipped_when_kb_already_satisfied_in_checkpoint() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_knowledge_base_ids=["kb-it"]),
        satisfied_required_knowledge_ids={"kb-it"},
    )
    assert actions == []


def test_prefetch_skipped_when_slot_gap_exists() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_slots=["device_model"],
        ),
    )
    assert actions == []


def test_prefetch_skipped_when_query_empty() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_knowledge_base_ids=["kb-it"], source_user_message="  "),
    )
    assert actions == []


# ---------------------------------------------------------------------------
# 场景 B：纯流转直通
# ---------------------------------------------------------------------------


def test_passthrough_transition_on_unique_unconditional_edge() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n3"
    assert action["reply_fragment"] == ""
    assert events[0][1]["scene"] == "passthrough_transition"


def test_passthrough_skipped_on_multiple_unconditional_edges() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=[
                {"next_node_id": "n3", "condition": ""},
                {"next_node_id": "n4", "condition": "default"},
            ]
        ),
    )
    assert actions == []


def test_passthrough_skipped_when_only_conditional_edges() -> None:
    """condition 是自由文本，无法静态求值——一律降级。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            allowed_transitions=[{"next_node_id": "n3", "condition": "需要外部商品数据"}]
        ),
    )
    assert actions == []


def test_passthrough_skipped_when_no_transitions() -> None:
    actions = plan_sop_prefill_actions(_sop_requirement(allowed_transitions=[]))
    assert actions == []


# ---------------------------------------------------------------------------
# 场景 A：缺槽直通
# ---------------------------------------------------------------------------


def test_await_user_on_missing_slots_without_required_capabilities() -> None:
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model", "urgency"]),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "awaiting_user"
    assert "device_model" in action["reply_fragment"]
    assert "urgency" in action["reply_fragment"]
    assert "提交申请" in action["reply_fragment"]
    assert events[0][1]["scene"] == "await_user_for_slots"


def test_await_user_uses_required_slots_when_expected_info_empty() -> None:
    requirement = _sop_requirement(required_slots=["device_model"])
    requirement.sop_context["step"]["expected_user_info"] = []
    actions = plan_sop_prefill_actions(requirement)
    assert len(actions) == 1
    assert "device_model" in actions[0]["reply_fragment"]


def test_await_user_skipped_when_required_capabilities_present() -> None:
    """节点有强制能力时不能直接问用户——可能先调工具就能补齐信息。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["device_model"],
            required_capability_names=["it_asset_tool"],
        ),
    )
    assert actions == []


# ---------------------------------------------------------------------------
# 降级边界
# ---------------------------------------------------------------------------


def test_conversation_kind_never_prefills() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(kind="conversation"),
    )
    assert actions == []


def test_unresolvable_step_degrades() -> None:
    actions = plan_sop_prefill_actions(
        _sop_requirement(sop_context={"skill_id": "sop-1"}),
    )
    assert actions == []


def test_same_step_resume_degrades() -> None:
    """断点恢复到同一步骤时 transcript 已有中间状态，不介入。"""

    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        same_step=True,
    )
    assert actions == []


def test_malformed_requirement_degrades_silently() -> None:
    class Broken:
        kind = "sop"

        @property
        def sop_context(self) -> Any:
            raise RuntimeError("boom")

    assert plan_sop_prefill_actions(Broken()) == []
    assert plan_sop_prefill_actions(None) == []


# ---------------------------------------------------------------------------
# 集成：挂载后纯流转节点零 LLM 完成
# ---------------------------------------------------------------------------


def test_harness_passthrough_node_completes_without_llm(monkeypatch) -> None:
    from app.core import harness_agent as harness_agent_module
    from app.db.models import ModelConfig

    def _fail_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("纯流转节点不应触发任何 LLM 调用")

    monkeypatch.setattr(harness_agent_module, "LLMClient", _fail_llm)

    trace_events: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("纯流转节点不应调用任何工具")

    result = HarnessTaskAgent().run(
        _sop_requirement(),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
        trace_sink=lambda t, p: trace_events.append((t, p)),
    )

    assert result.status == "completed"
    assert result.next_step_id == "n3"
    assert result.action_count == 1
    planned = next(
        payload for event_type, payload in trace_events if event_type == TRACE_EVENT
    )
    assert planned["scene"] == "passthrough_transition"


# ---------------------------------------------------------------------------
# 集成：预检索动作真实执行并满足 required_knowledge_base_ids
# ---------------------------------------------------------------------------


def test_harness_prefetch_executes_and_satisfies_required_knowledge(monkeypatch) -> None:
    from app.core import harness_agent as harness_agent_module
    from app.core.task_request_compiler import CapabilityDescriptor
    from app.db.models import ModelConfig

    llm_calls: list[dict[str, Any]] = []

    class FakeLLMClient:
        def __init__(self, _model_config: ModelConfig) -> None:
            pass

        def generate_json(
            self, system_prompt: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            llm_calls.append(payload)
            return {
                "action": "finish",
                "status": "completed",
                "reply_fragment": "已根据知识库回答。",
                "task_summary": "完成。",
            }

    monkeypatch.setattr(harness_agent_module, "LLMClient", FakeLLMClient)

    trace_events: list[tuple[str, dict[str, Any]]] = []

    def _invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "knowledge_search"
        return {
            "success": True,
            "data": {"evidence_pack": [{"content": "申领流程：..."}]},
            "citations": [{"source": "kb-it"}],
        }

    result = HarnessTaskAgent().run(
        _sop_requirement(
            required_knowledge_base_ids=["kb-it"],
            required_capability_names=["knowledge_search"],
            capability_manifest=CapabilityManifest(
                available=[
                    CapabilityDescriptor(
                        capability_id="ks",
                        name="knowledge_search",
                        kind="internal",
                    )
                ]
            ),
        ),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
        trace_sink=lambda t, p: trace_events.append((t, p)),
    )

    # 预检索执行后 LLM 首轮直接面对证据收尾：仅 1 次 LLM 调用
    assert result.status == "completed"
    assert result.reply_fragment == "已根据知识库回答。"
    assert len(llm_calls) == 1
    planned = next(
        payload for event_type, payload in trace_events if event_type == TRACE_EVENT
    )
    assert planned["scene"] == "prefetch_knowledge"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
