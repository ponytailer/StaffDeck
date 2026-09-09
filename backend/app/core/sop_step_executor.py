"""SOP 确定性步骤执行器（P1）。

Harness 循环里每个节点推进都要一整轮 task_action LLM 决策（25-187s/轮），
但其中三类场景的决策是纯代码可判定的——SOP 图结构（skill_schema.SkillCard）
在 TaskRequirement 编译期已包含全部判定所需信息：

- **C 预检索**：节点声明了强制知识库且槽位齐 → 直接产出 knowledge_search
  动作，跳过「LLM 决策调检索」那一轮。
- **B 纯流转直通**：节点无槽位缺口、无强制能力、且有唯一无条件出边 →
  直接 finish(completed, next_step_id)，外层 continue_frame 循环会立即
  编译下一节点继续，整个节点零 LLM。
- **A 缺槽直通**：节点缺槽位且无强制能力 → 直接 finish(awaiting_user)，
  问话用模板文案（LLM 原本那轮只为把这句话说得更自然，P2 再用轻量模型润色）。

设计约束：
- 只走无条件边（condition 为空/default/else）；condition 是自由文本
  （如「需要外部商品数据」），无法静态求值，一律降级。
- 断点恢复到同一步骤（same_step）时不介入，避免重复动作。
- 任何异常返回空列表，harness 侧静默回退现有 LLM 决策链路。
- 返回动作 dict（HarnessAction 兼容），由 harness_agent 侧
  model_validate——独立模块避免循环 import。
"""

from __future__ import annotations

from typing import Any

from app.core.graph_rules import GraphRules

# 允许确定性直通的出边条件（与 GraphRules.edge_condition 的空值语义一致）
_UNCONDITIONAL_EDGE_CONDITIONS = {"", "default", "else"}

_QUERY_MAX_CHARS = 200

# trace 事件类型，供 diagnose 脚本统计 P1 生效率
TRACE_EVENT = "sop_prefill_planned"


def plan_sop_prefill_actions(
    requirement: Any,
    *,
    same_step: bool = False,
    satisfied_required_knowledge_ids: set[str] | None = None,
    trace_sink: Any = None,
) -> list[dict[str, Any]]:
    """为 sop TaskRequirement 产出确定性动作序列；不可判定时返回空列表。

    动作以 dict 形式返回（HarnessAction.model_validate 兼容），三个场景
    互斥，按 C → B → A 优先级取第一个命中的。
    """

    try:
        if requirement is None or getattr(requirement, "kind", "") != "sop":
            return []
        if same_step:
            # 断点恢复到同一步骤：transcript 已有中间状态，预填可能重复
            return []

        sop_context = getattr(requirement, "sop_context", None)
        step = sop_context.get("step") if isinstance(sop_context, dict) else None
        if not isinstance(step, dict) or not step:
            return []

        required_slots = [str(item) for item in getattr(requirement, "required_slots", []) or []]
        required_capabilities = [
            str(item) for item in getattr(requirement, "required_capability_names", []) or []
        ]
        required_knowledge_ids = [
            str(item)
            for item in getattr(requirement, "required_knowledge_base_ids", []) or []
        ]

        # 场景 C：预检索
        if required_knowledge_ids and not required_slots:
            already = satisfied_required_knowledge_ids or set()
            pending_kb_ids = [kb for kb in required_knowledge_ids if kb not in already]
            if pending_kb_ids:
                query = str(getattr(requirement, "source_user_message", "") or "").strip()
                if query:
                    action = _knowledge_search_action(query, pending_kb_ids)
                    return _emit(
                        trace_sink,
                        "prefetch_knowledge",
                        [action],
                        query=query[:_QUERY_MAX_CHARS],
                        knowledge_base_ids=pending_kb_ids,
                    )

        # 场景 C 之后的能力场景都要求无强制能力
        if required_capabilities or required_knowledge_ids:
            return []

        if required_slots:
            # 场景 A：缺槽直通
            action = _await_user_action(step, required_slots)
            return _emit(
                trace_sink,
                "await_user_for_slots",
                [action],
                required_slots=required_slots,
            )

        # 场景 B：纯流转直通
        next_node_id = _unique_unconditional_next(
            getattr(requirement, "allowed_transitions", []) or []
        )
        if next_node_id:
            action = _passthrough_action(step, next_node_id)
            return _emit(
                trace_sink,
                "passthrough_transition",
                [action],
                next_node_id=next_node_id,
            )
        return []
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路，一律降级
        return []


def _unique_unconditional_next(allowed_transitions: list[Any]) -> str:
    """唯一无条件出边时返回目标节点；有歧义或全是条件边则返回空串。"""

    unconditional: list[str] = []
    for edge in allowed_transitions:
        if not isinstance(edge, dict):
            continue
        condition = str(edge.get("condition") or "").strip().lower()
        if condition not in _UNCONDITIONAL_EDGE_CONDITIONS:
            continue
        target = str(edge.get("next_node_id") or "").strip()
        if target and target not in unconditional:
            unconditional.append(target)
    if len(unconditional) == 1:
        return unconditional[0]
    return ""


def _knowledge_search_action(query: str, knowledge_base_ids: list[str]) -> dict[str, Any]:
    return {
        "action": "tool",
        "tool_name": "knowledge_search",
        "arguments": {
            "query": query[:_QUERY_MAX_CHARS],
            "knowledge_base_ids": knowledge_base_ids,
            "max_chunks": 8,
        },
        "task_summary": "SOP 确定性预检索（跳过 LLM 决策轮）。",
    }


def _passthrough_action(step: dict[str, Any], next_node_id: str) -> dict[str, Any]:
    step_name = str(step.get("name") or step.get("node_id") or "").strip()
    return {
        "action": "finish",
        "status": "completed",
        "next_step_id": next_node_id,
        "reply_fragment": "",
        "task_summary": f"SOP 步骤「{step_name}」无待办事项，确定性流转到下一节点。",
    }


def _await_user_action(step: dict[str, Any], required_slots: list[str]) -> dict[str, Any]:
    step_name = str(step.get("name") or "").strip()
    expected = [str(item).strip() for item in step.get("expected_user_info") or [] if str(item).strip()]
    missing = expected or required_slots
    step_part = f"继续「{step_name}」" if step_name else "继续当前步骤"
    reply = f"为了{step_part}，请提供：{'、'.join(missing)}。"
    return {
        "action": "finish",
        "status": "awaiting_user",
        "reply_fragment": reply,
        "task_summary": "SOP 步骤缺少必填信息，确定性发起用户询问。",
        "slot_updates": {},
    }


def _emit(
    trace_sink: Any,
    scene: str,
    actions: list[dict[str, Any]],
    **details: Any,
) -> list[dict[str, Any]]:
    if callable(trace_sink):
        try:
            trace_sink(
                TRACE_EVENT,
                {
                    "scene": scene,
                    "action_count": len(actions),
                    "actions": [item.get("action") for item in actions],
                    **details,
                },
            )
        except Exception:  # noqa: BLE001 - 观测失败不影响主链路
            pass
    return actions


# GraphRules.slot_satisfied 供 harness 侧/测试复用（保持单一实现来源）
slot_satisfied = GraphRules.slot_satisfied
