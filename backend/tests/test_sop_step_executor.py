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
    step: dict[str, Any] = {
        "node_id": "n2",
        "name": "提交申请",
        "instruction": "提交申请单",
        "expected_user_info": ["device_model", "urgency"],
    }
    if "expected_user_info" in overrides:
        step["expected_user_info"] = overrides.pop("expected_user_info")
    base: dict[str, Any] = {
        "task_frame_id": "task-1",
        "kind": "sop",
        "goal": "完成 SOP 步骤",
        "source_user_message": "帮我申领一台电脑",
        "sop_context": {
            "skill_id": "sop-1",
            "skill_name": "IT 服务",
            "step": step,
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


# ---------------------------------------------------------------------------
# 恢复补槽会话（same_step + 上次终点 awaiting_user）放行场景 A
# ---------------------------------------------------------------------------


def test_resume_awaiting_user_extraction_completes_then_passthrough(
    monkeypatch,
) -> None:
    """用户回来补槽位（如 15:28 复测场景）：抽齐 → 直通流转，不再 7 轮 LLM。"""
    import app.core.sop_step_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {
            "employee_id": "3012",
            "system": "OA系统",
            "permission": "管理员",
            "access_level": "生产环境管理员",
        },
    )
    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
        slot_extraction_model="fake-model",
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["slot_updates"]["employee_id"] == "3012"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["passthrough_transition_after_extraction"]


def test_resume_awaiting_user_partial_extraction_asks_missing_only(
    monkeypatch,
) -> None:
    """恢复补槽但只抽到部分：只问缺的，已抽值落库防重复问。"""
    import app.core.sop_step_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"employee_id": "3012"},
    )
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert "system" in action["reply_fragment"]
    assert "employee_id" not in action["reply_fragment"]
    assert action["slot_updates"] == {"employee_id": "3012"}


def test_resume_awaiting_user_blocks_prefetch_and_passthrough() -> None:
    """恢复补槽会话不重复预检索（C）也不做流转直通（B）。"""
    # 场景 C：kb 必填且无槽位缺口 → 恢复场景下不介入
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(required_knowledge_base_ids=["kb-it"]),
            same_step=True,
            resumed_awaiting_user=True,
        )
        == []
    )
    # 场景 B：无槽位无 kb 的纯流转 → 恢复场景下不介入
    assert (
        plan_sop_prefill_actions(
            _sop_requirement(),
            same_step=True,
            resumed_awaiting_user=True,
        )
        == []
    )


def test_resume_awaiting_user_with_kb_slots_step_degrades() -> None:
    """恢复补槽 + 步骤同时要求 kb：抽取结果无法安全推进，退回现有链路。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["device_model"],
            required_knowledge_base_ids=["kb-it"],
        ),
        same_step=True,
        resumed_awaiting_user=True,
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


# ---------------------------------------------------------------------------
# P2：场景 A 前置轻量槽位抽取
# ---------------------------------------------------------------------------


def test_slot_extraction_completes_slots_then_passthrough(monkeypatch) -> None:
    """复现截图 bug 场景：用户首条消息已带齐信息 → 抽齐后直通，不再错误询问。"""
    import app.core.sop_step_executor as executor_module

    def fake_extract(*args, **kwargs):
        assert "employee_id" in args[0] or "employee_id" in kwargs.get("fields", [])
        assert kwargs.get("step_instruction") == "" or "step_instruction" in kwargs
        return {
            "employee_id": "3012",
            "system": "OA系统",
            "permission": "管理员",
            "access_level": "生产环境管理员",
        }

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fake_extract)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
            known_slots={},
        ),
        slot_extraction_model="fake-model",
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n3"
    assert action["slot_updates"]["employee_id"] == "3012"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["passthrough_transition_after_extraction"]


def test_slot_extraction_partial_asks_missing_only(monkeypatch) -> None:
    """部分抽取：只问缺的字段，已抽值随 slot_updates 落库防重复问。"""
    import app.core.sop_step_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"employee_id": "3012"},
    )

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert "system" in action["reply_fragment"]
    assert "access_level" in action["reply_fragment"]
    assert "employee_id" not in action["reply_fragment"]
    assert action["slot_updates"] == {"employee_id": "3012"}


def test_slot_extraction_failure_falls_back_to_template(monkeypatch) -> None:
    """抽取返回空结果退回模板直通（_extract_slots_llm 内部兜异常返回 {}）。"""
    import app.core.sop_step_executor as executor_module

    monkeypatch.setattr(executor_module, "_extract_slots_llm", lambda *args, **kwargs: {})
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        slot_extraction_model="fake-model",
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "awaiting_user"
    assert actions[0]["slot_updates"] == {}


def test_unexpected_executor_error_degrades_to_empty(monkeypatch) -> None:
    """执行器实现层异常：主流程整体返回空 → harness 走现有 LLM 链路。"""
    import app.core.sop_step_executor as executor_module

    def boom(*args):
        raise RuntimeError("bug")

    monkeypatch.setattr(executor_module, "_extract_slots_llm", boom)
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
        slot_extraction_model="fake-model",
    )
    assert actions == []


def test_slot_extraction_filters_unknown_fields(monkeypatch) -> None:
    """抽取返回的未知字段被过滤，不进入 slot_updates。"""
    import app.core.sop_step_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_extract_slots_llm",
        lambda *args, **kwargs: {"device_model": "ThinkPad", "hacker_field": "x"},
    )
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model", "urgency"]),
        slot_extraction_model="fake-model",
    )
    action = actions[0]
    assert action["status"] == "awaiting_user"
    assert action["slot_updates"] == {"device_model": "ThinkPad"}


def test_slot_extraction_skipped_without_model() -> None:
    """未传模型（如单测环境）：跳过抽取，行为与 P1 模板直通一致。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(required_slots=["device_model"]),
    )
    assert actions[0]["status"] == "awaiting_user"


def test_slot_extraction_receives_step_instruction(monkeypatch) -> None:
    """节点说明作为 field_context 传给抽取调用，供模型理解抽象字段语义。"""
    import app.core.sop_step_executor as executor_module

    captured: dict[str, Any] = {}

    def fake_extract(*args, **kwargs):
        captured["fields"] = args[0]
        captured["step_instruction"] = kwargs.get("step_instruction")
        captured["model_config"] = args[3]
        return {"employee_id": "1002", "system": "crm"}

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fake_extract)
    requirement = _sop_requirement(
        required_slots=["employee_id", "system", "permission", "access_level"],
        expected_user_info=["employee_id", "system", "permission", "access_level"],
    )
    requirement.sop_context["step"]["instruction"] = (
        "收集员工工号、目标系统、权限级别及访问级别/环境类型。"
    )
    plan_sop_prefill_actions(
        requirement,
        slot_extraction_model="fake-model",
    )
    assert captured["step_instruction"] == (
        "收集员工工号、目标系统、权限级别及访问级别/环境类型。"
    )
    assert captured["model_config"] == "fake-model"


# ---------------------------------------------------------------------------
# P3 场景 D：decision 节点分支条件槽位直判
# ---------------------------------------------------------------------------

_DECISION_STEP = {
    "node_id": "n2",
    "type": "decision",
    "name": "权限级别分流判断",
    "instruction": "根据 access_level/permission 判断是否高权限。",
}

_ROUTE_EDGES = [
    {"next_node_id": "n3", "condition": "普通权限"},
    {"next_node_id": "n6", "condition": "高权限/生产环境/敏感数据"},
]


def test_decision_direct_eval_unique_match_transitions(monkeypatch) -> None:
    """复现 16:0x 场景：access_level=生产环境 唯一命中高权限边 → 零 LLM 直判。"""
    import app.core.sop_step_executor as executor_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("直判命中不应触发槽位抽取/LLM")

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fail_llm)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "skill_name": "权限开通工单分流", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={
                "employee_id": "1002",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "completed"
    assert action["next_step_id"] == "n6"
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["decision_direct_eval"]


def test_decision_direct_eval_ambiguous_falls_back() -> None:
    """泛化取值同时命中两条边（「权限」⊂ 普通权限/高权限）→ 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"permission": "权限"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_no_match_falls_back() -> None:
    """槽位与任何分支都无证据交集 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"employee_id": "1002"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_negation_edge_never_selected() -> None:
    """含否定词的边不参与直判：高权限命中正向边，而非高权限边永不被选。"""
    edges = [
        {"next_node_id": "n6", "condition": "高权限"},
        {"next_node_id": "n3", "condition": "非高权限"},
    ]
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=edges,
            known_slots={"access_level": "高权限"},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["next_step_id"] == "n6"
    # 反向：普通权限槽位无法为「非高权限」提供正向证据 → 降级
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=edges,
            known_slots={"access_level": "普通查询"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_requires_decision_type() -> None:
    """非 decision 类型节点不直判（保守门控）。"""
    step = dict(_DECISION_STEP, type="collect_info")
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": step},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
        ),
    )
    assert actions == []


def test_decision_direct_eval_blocked_on_resume_recovery() -> None:
    """恢复补槽会话不做决策直判（只允许场景 A）。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _DECISION_STEP},
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
        ),
        same_step=True,
        resumed_awaiting_user=True,
    )
    assert actions == []


# ---------------------------------------------------------------------------
# P4 回归：脏槽位/消息冲突下的决策直判安全（17:17 截图复现）
# ---------------------------------------------------------------------------

_SLOT_FIELDS = ["employee_id", "system", "permission", "access_level", "permission_level"]


def test_decision_direct_eval_ignores_dirty_slots() -> None:
    """复现 17:17 截图：脏槽位 permission_preference 命中高权限边、合法字段
    permission=normal（英文）对不上中文条件 → 白名单过滤后 0 命中 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _DECISION_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={
                "employee_id": "1003",
                "system": "crm",
                "permission": "normal",
                "permission_level": "normal",
                "permission_preference": "倾向于申请生产环境的管理员权限",
            },
        ),
    )
    assert actions == []


def test_decision_direct_eval_message_conflict_veto() -> None:
    """合法槽位命中一条边，但用户消息逐字提到另一条边的条件 → 冲突否决。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _DECISION_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=_ROUTE_EDGES,
            known_slots={"access_level": "生产环境"},
            source_user_message="申请生产环境crm的普通权限，工号1003",
        ),
    )
    assert actions == []


def test_handoff_summary_filters_dirty_slots() -> None:
    """F 回复摘要只含技能声明字段，脏槽位（permission_preference）不出现。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={
                "skill_id": "sop-1",
                "step": _HANDOFF_STEP,
                "slot_fields": _SLOT_FIELDS,
            },
            allowed_transitions=[],
            known_slots={
                "employee_id": "1001",
                "system": "crm",
                "permission_preference": "倾向于申请生产环境的管理员权限",
            },
        ),
    )
    assert len(actions) == 1
    reply = actions[0]["reply_fragment"]
    assert "1001" in reply
    assert "permission_preference" not in reply
    assert "倾向于" not in reply


# ---------------------------------------------------------------------------
# P4 场景 F：handoff 终点节点直通（零 LLM 转人工）
# ---------------------------------------------------------------------------

_HANDOFF_STEP = {
    "node_id": "n6",
    "type": "handoff",
    "name": "高权限人工审批",
    "instruction": "高权限申请转人工处理。",
}


def test_handoff_terminal_node_passthrough(monkeypatch) -> None:
    """终点 handoff 节点：槽位齐 + 无出边 → 零 LLM 直接 finish(handoff)。"""
    import app.core.sop_step_executor as executor_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("handoff 直通不应触发槽位抽取/LLM")

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fail_llm)

    events = _events()
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "skill_name": "权限开通工单分流", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={
                "employee_id": "1001",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "finish"
    assert action["status"] == "handoff"
    assert action.get("next_step_id") in (None, "")
    assert "1001" in action["reply_fragment"]
    assert "crm" in action["reply_fragment"]
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["handoff_passthrough"]


def test_handoff_with_transitions_not_direct(monkeypatch) -> None:
    """handoff 节点带唯一无条件出边（非终点）→ 走场景 B 流转，不走 F。"""
    import app.core.sop_step_executor as executor_module

    def fail_llm(*args, **kwargs):
        raise AssertionError("不应触发抽取")

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fail_llm)

    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[{"next_node_id": "n7", "condition": ""}],
            known_slots={"employee_id": "1001"},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "completed"
    assert actions[0]["next_step_id"] == "n7"


def test_handoff_blocked_on_resume_recovery() -> None:
    """恢复补槽会话不做 handoff 直通（只允许场景 A）。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={"employee_id": "1001"},
        ),
        same_step=True,
        resumed_awaiting_user=True,
    )
    assert actions == []


def test_handoff_without_slots_uses_generic_reply() -> None:
    """无已知槽位时用通用话术，不出现空括号。"""
    actions = plan_sop_prefill_actions(
        _sop_requirement(
            sop_context={"skill_id": "sop-1", "step": _HANDOFF_STEP},
            allowed_transitions=[],
            known_slots={},
        ),
    )
    assert len(actions) == 1
    assert actions[0]["status"] == "handoff"
    assert "（" not in actions[0]["reply_fragment"]


# ---------------------------------------------------------------------------
# P3 场景 E：tool_call 节点按 input_schema 槽位直组参数
# ---------------------------------------------------------------------------


def _tool_requirement(**overrides: Any) -> Any:
    from app.core.task_request_compiler import CapabilityDescriptor

    step = {
        "node_id": "n4",
        "type": "tool_call",
        "name": "调用权限开通接口",
        "instruction": "工具参数满足时直接调用 it.grant_permission 工具。",
    }
    if "step" in overrides:
        step = overrides.pop("step")
    manifest = overrides.pop(
        "capability_manifest",
        CapabilityManifest(
            available=[
                CapabilityDescriptor(
                    capability_id="cap-1",
                    name="it.grant_permission",
                    kind="tool",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "system": {"type": "string"},
                            "permission": {"type": "string"},
                            "access_level": {"type": "string"},
                        },
                        "required": ["system", "permission"],
                    },
                )
            ]
        ),
    )
    return _sop_requirement(
        sop_context={"skill_id": "sop-1", "step": step},
        capability_manifest=manifest,
        **overrides,
    )


def test_tool_direct_call_composes_args_from_slots() -> None:
    """schema 必填参数全部可由槽位满足 → 直接产出 tool 动作。"""
    events = _events()
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            known_slots={
                "employee_id": "1002",
                "system": "crm",
                "permission": "管理员",
                "access_level": "生产环境",
            },
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action["action"] == "tool"
    assert action["tool_name"] == "it.grant_permission"
    assert action["arguments"] == {
        "system": "crm",
        "permission": "管理员",
        "access_level": "生产环境",
    }
    scenes = [p["scene"] for t, p in events if t == TRACE_EVENT]
    assert scenes == ["tool_direct_call"]


def test_tool_direct_call_missing_required_falls_back() -> None:
    """必填参数无槽位可组 → 降级 LLM，并留下 skipped 观测。"""
    events = _events()
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            known_slots={"employee_id": "1002"},
        ),
        trace_sink=lambda t, p: events.append((t, p)),
    )
    assert actions == []
    skipped = [p for t, p in events if t == TRACE_EVENT]
    assert skipped and skipped[0]["scene"] == "tool_direct_call_skipped"
    assert skipped[0]["missing_required"] == ["system", "permission"]


def test_tool_direct_call_requires_tool_call_type() -> None:
    step = {"node_id": "n4", "type": "decision", "name": "x"}
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            step=step,
            required_capability_names=["it.grant_permission"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_tool_direct_call_skipped_when_slots_missing() -> None:
    """槽位缺口未解除时不直组（先走收集/抽取链路）。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission"],
            required_slots=["system"],
            known_slots={},
        ),
    )
    assert actions == []


def test_tool_direct_call_ambiguous_capabilities_fall_back() -> None:
    """多个候选能力时不猜 → 降级 LLM。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["it.grant_permission", "it.ticket_create"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_tool_direct_call_skips_internal_tools() -> None:
    """内置能力（knowledge_search 等）不走参数直组。"""
    actions = plan_sop_prefill_actions(
        _tool_requirement(
            required_capability_names=["knowledge_search"],
            known_slots={"system": "crm", "permission": "管理员"},
        ),
    )
    assert actions == []


def test_harness_full_message_turn_completes_without_task_action_llm(monkeypatch) -> None:
    """集成（截图 bug 端到端）：首条消息带齐信息 + 抽齐 + 纯流转 →
    仅一次轻量抽取调用，零 task_action LLM 轮。"""
    from app.core import harness_agent as harness_agent_module
    from app.core import sop_step_executor as executor_module
    from app.db.models import ModelConfig

    def _fail_llm(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("抽取后直通不应触发 task_action LLM")

    monkeypatch.setattr(harness_agent_module, "LLMClient", _fail_llm)

    extract_calls: list[list[str]] = []

    def fake_extract(*args, **kwargs):
        extract_calls.append(list(kwargs.get("fields") or args[0]))
        return {"employee_id": "3012", "system": "OA", "permission": "管理员", "access_level": "生产"}

    monkeypatch.setattr(executor_module, "_extract_slots_llm", fake_extract)

    def _invoke_tool(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("不应调用工具")

    result = HarnessTaskAgent().run(
        _sop_requirement(
            required_slots=["employee_id", "system", "permission", "access_level"],
            expected_user_info=["employee_id", "system", "permission", "access_level"],
        ),
        ModelConfig(
            tenant_id="t", name="m", provider="openai", base_url="http://x/v1",
            api_key_encrypted="x", model_name="gpt", purpose="chat",
        ),
        _invoke_tool,
        max_actions=3,
    )

    assert result.status == "completed"
    assert result.next_step_id == "n3"
    assert result.slot_updates["employee_id"] == "3012"
    assert len(extract_calls) == 1
    # task_action LLM 轮次数：0（轻量抽取不算 task_action）


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


