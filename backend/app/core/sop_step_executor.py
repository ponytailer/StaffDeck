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

import re
from typing import Any

from app.core.graph_rules import GraphRules

# 允许确定性直通的出边条件（与 GraphRules.edge_condition 的空值语义一致）
_UNCONDITIONAL_EDGE_CONDITIONS = {"", "default", "else"}

_QUERY_MAX_CHARS = 200

# trace 事件类型，供 diagnose 脚本统计 P1 生效率
TRACE_EVENT = "sop_prefill_planned"

# P3 场景 D/E：节点类型门控（与 skill_schema.SkillGraphNode.type 对齐）
_DECISION_NODE_TYPE = "decision"
_TOOL_CALL_NODE_TYPE = "tool_call"
# 场景 F：人工处理终点节点
_HANDOFF_NODE_TYPE = "handoff"

# 场景 D：含否定语义的分支不做证据匹配（「非高权限」无法用正向包含判定）
_NEGATION_MARKERS = ("非", "不", "排除", "除外", "无法")
_CONDITION_TOKEN_SPLIT = re.compile(r"[/、，,;；\s]+")
# 双向包含匹配的最小词元长度（过滤「权」这类单字误配）
_MIN_TOKEN_CHARS = 2

# 场景 E：内置能力不走参数直组（有自己的执行通道/渐进披露协议）
_INTERNAL_TOOL_NAMES = {
    "capability_search",
    "capability_describe",
    "knowledge_search",
    "list_published_deliverables",
    "read_published_deliverable",
}


def plan_sop_prefill_actions(
    requirement: Any,
    *,
    same_step: bool = False,
    resumed_awaiting_user: bool = False,
    satisfied_required_knowledge_ids: set[str] | None = None,
    trace_sink: Any = None,
    slot_extraction_model: Any = None,
) -> list[dict[str, Any]]:
    """为 sop TaskRequirement 产出确定性动作序列；不可判定时返回空列表。

    动作以 dict 形式返回（HarnessAction.model_validate 兼容），三个场景
    互斥，按 C → B → A 优先级取第一个命中的。场景 A 会用
    slot_extraction_model（轻量意图模型优先）做一次槽位抽取——用户首条
    消息往往已带齐信息，避免「已提供还要求补全」的错误询问。

    resumed_awaiting_user：断点恢复到同一步骤且上次终点是本执行器发出的
    awaiting_user（等槽位）。此时用户回来补信息正是槽位抽取的主场景，
    放行场景 A；预检索/流转（C/B）仍不介入，避免重复动作。
    """

    try:
        if requirement is None or getattr(requirement, "kind", "") != "sop":
            return []
        resume_slot_recovery = bool(same_step and resumed_awaiting_user)
        if same_step and not resume_slot_recovery:
            # 断点恢复到同一步骤：transcript 已有中间状态，预填可能重复动作
            return []

        sop_context = getattr(requirement, "sop_context", None)
        step = sop_context.get("step") if isinstance(sop_context, dict) else None
        if not isinstance(step, dict) or not step:
            return []
        # 技能全图声明的槽位字段（编译期从各节点 expected_user_info 并集而来），
        # 作为路由证据/回复摘要的字段白名单，隔离会话历史遗留的脏槽位。
        slot_fields = (
            [
                str(item).strip()
                for item in (sop_context.get("slot_fields") or [])
                if str(item).strip()
            ]
            if isinstance(sop_context, dict)
            else []
        )

        required_slots = [str(item) for item in getattr(requirement, "required_slots", []) or []]
        required_capabilities = [
            str(item) for item in getattr(requirement, "required_capability_names", []) or []
        ]
        required_knowledge_ids = [
            str(item)
            for item in getattr(requirement, "required_knowledge_base_ids", []) or []
        ]
        known_slots = dict(getattr(requirement, "known_slots", {}) or {})

        # 场景 C：预检索（仅在无槽位缺口时；缺槽场景由 A 抽齐后重判）。
        # 恢复补槽会话不做预检索，避免与已完成的检索重复。
        if (
            not resume_slot_recovery
            and required_knowledge_ids
            and not required_slots
        ):
            return _plan_prefetch_or_passthrough(
                requirement,
                required_knowledge_ids=required_knowledge_ids,
                satisfied_required_knowledge_ids=satisfied_required_knowledge_ids,
                extracted_slots={},
                step=step,
                trace_sink=trace_sink,
                scene_suffix="",
            )

        # 场景 C 之后：有强制能力/知识时仅剩场景 E（tool_call 节点参数直组）
        if required_capabilities or required_knowledge_ids:
            return _plan_tool_direct(
                requirement,
                step=step,
                required_capabilities=required_capabilities,
                required_slots=required_slots,
                required_knowledge_ids=required_knowledge_ids,
                known_slots=known_slots,
                trace_sink=trace_sink,
            )

        if required_slots:
            # 场景 A（P2 增强）：缺槽时先尝试轻量 LLM 槽位抽取——
            # 用户首条消息往往已带齐信息（如「申请OA管理员权限，工号3012」），
            # 编译期 required_slots 只看 session.slots_json 会误判缺槽。
            expected_fields = [
                str(item).strip()
                for item in (step.get("expected_user_info") or [])
                if str(item).strip()
            ]
            extracted = _extract_slots_llm(
                expected_fields or required_slots,
                str(getattr(requirement, "source_user_message", "") or ""),
                known_slots,
                slot_extraction_model,
                trace_sink,
                step_instruction=str(step.get("instruction") or ""),
            )
            extracted = {
                str(key): value
                for key, value in extracted.items()
                if str(key) in set(expected_fields or required_slots)
                and value not in (None, "", [], {})
            }
            missing = [slot for slot in required_slots if slot not in extracted]
            if not missing:
                # 抽齐：槽位缺口解除 → 重走 C/B 判定（此时 kb 必为空，实际走 B）
                return _plan_prefetch_or_passthrough(
                    requirement,
                    required_knowledge_ids=required_knowledge_ids,
                    satisfied_required_knowledge_ids=satisfied_required_knowledge_ids,
                    extracted_slots=extracted,
                    step=step,
                    trace_sink=trace_sink,
                    scene_suffix="_after_extraction",
                )
            # 部分/未抽到：问缺的，已抽到的值随 slot_updates 落库防重复问
            action = _await_user_action(step, missing)
            action["slot_updates"] = extracted
            return _emit(
                trace_sink,
                "await_user_for_slots",
                [action],
                required_slots=missing,
                extracted_slots=extracted,
            )

        if resume_slot_recovery:
            # 恢复补槽会话只允许场景 A：无可抽槽位时不做流转/决策直判
            return []

        # 场景 F（P4）：handoff 终点节点直通。槽位无缺口、无强制能力/知识、
        # 无出边（终点）时，转人工是纯协议动作——模板回复带槽位摘要即可，
        # 跳过纯协议 task_action LLM 轮（实测 12-17s/轮，转交节点连烧 3 轮）。
        if (
            str(step.get("type") or "").strip() == _HANDOFF_NODE_TYPE
            and not (getattr(requirement, "allowed_transitions", None) or [])
        ):
            return _plan_handoff_passthrough(
                step,
                known_slots,
                trace_sink,
                allowed_fields=set(slot_fields),
            )

        # 场景 D（P3）：decision 节点的分支条件能用槽位取值直接证定时，
        # 跳过 LLM 决策轮（此前该轮还常附带多余的 capability_describe/建单）
        if str(step.get("type") or "").strip() == _DECISION_NODE_TYPE:
            next_target = _decision_direct_next(
                getattr(requirement, "allowed_transitions", []) or [],
                known_slots,
                user_message=str(getattr(requirement, "source_user_message", "") or ""),
                allowed_fields=set(slot_fields),
            )
            if next_target:
                action = _passthrough_action(step, next_target)
                return _emit(
                    trace_sink,
                    "decision_direct_eval",
                    [action],
                    next_node_id=next_target,
                )

        # 场景 B：纯流转直通
        return _plan_prefetch_or_passthrough(
            requirement,
            required_knowledge_ids=[],
            satisfied_required_knowledge_ids=satisfied_required_knowledge_ids,
            extracted_slots={},
            step=step,
            trace_sink=trace_sink,
            scene_suffix="",
        )
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路，一律降级
        return []


def _plan_prefetch_or_passthrough(
    requirement: Any,
    *,
    required_knowledge_ids: list[str],
    satisfied_required_knowledge_ids: set[str] | None,
    extracted_slots: dict[str, Any],
    step: dict[str, Any],
    trace_sink: Any,
    scene_suffix: str,
) -> list[dict[str, Any]]:
    """槽位无缺口后的两个确定性分支：C 预检索 / B 纯流转直通。

    场景 A 抽齐槽位后也走这里（scene_suffix="_after_extraction" 供观测
    区分抽取后直通的次数）。
    """

    # C：预检索
    if required_knowledge_ids:
        already = satisfied_required_knowledge_ids or set()
        pending_kb_ids = [kb for kb in required_knowledge_ids if kb not in already]
        query = str(getattr(requirement, "source_user_message", "") or "").strip()
        if pending_kb_ids and query:
            action = _knowledge_search_action(query, pending_kb_ids)
            return _emit(
                trace_sink,
                f"prefetch_knowledge{scene_suffix}",
                [action],
                query=query[:_QUERY_MAX_CHARS],
                knowledge_base_ids=pending_kb_ids,
            )
        return []

    # B：纯流转直通（抽取值随 slot_updates 落库）
    next_node_id = _unique_unconditional_next(
        getattr(requirement, "allowed_transitions", []) or []
    )
    if next_node_id:
        action = _passthrough_action(step, next_node_id)
        if extracted_slots:
            action["slot_updates"] = extracted_slots
        return _emit(
            trace_sink,
            f"passthrough_transition{scene_suffix}",
            [action],
            next_node_id=next_node_id,
            extracted_slots=extracted_slots,
        )
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


def _decision_direct_next(
    allowed_transitions: list[Any],
    known_slots: dict[str, Any],
    *,
    user_message: str = "",
    allowed_fields: set[str] | None = None,
) -> str:
    """场景 D：用槽位取值为分支条件找唯一证据边。

    规则（保守优先，任何歧义都交还 LLM）：
    - 只在**恰好一条**条件边命中时直判；0 条或多条命中返回空串。
    - 证据只来自已知槽位值（LLM 抽取过的规范化表达），不用用户原话，
      避免否定句（「不是管理员」）误配。
    - allowed_fields 非空时证据只取技能声明的字段（slot_fields）——
      会话历史遗留的脏槽位（如上一轮的 permission_preference）不参与判定。
    - 冲突否决：未被槽位命中的边，若其条件词元逐字出现在用户消息里
      （如用户明说「普通权限」），说明语义有歧义，交还 LLM。
    - 含否定语义（非/不/排除…）的边整体跳过，永远不由直判选出。
    - 匹配为双向包含（token⊆值 或 值⊆token），词元/取值至少 2 字。
    """

    if not isinstance(allowed_transitions, list) or not known_slots:
        return ""
    allowed = {str(item).strip() for item in (allowed_fields or set()) if str(item).strip()}
    values: list[str] = []
    for key, value in known_slots.items():
        if allowed and str(key).strip() not in allowed:
            # 非技能声明字段（历史遗留/LLM 自创槽位）不作为路由证据
            continue
        text = str(value).strip().lower()
        if len(text) >= _MIN_TOKEN_CHARS:
            values.append(text)
    if not values:
        return ""
    conditional: list[tuple[str, list[str]]] = []
    for edge in allowed_transitions:
        if not isinstance(edge, dict):
            continue
        target = str(edge.get("next_node_id") or "").strip()
        condition = str(edge.get("condition") or "").strip()
        if (
            not target
            or not condition
            or condition.lower() in _UNCONDITIONAL_EDGE_CONDITIONS
        ):
            continue
        if any(marker in condition for marker in _NEGATION_MARKERS):
            continue
        tokens = [
            token
            for token in (part.strip().lower() for part in _CONDITION_TOKEN_SPLIT.split(condition))
            if len(token) >= _MIN_TOKEN_CHARS
        ]
        if tokens:
            conditional.append((target, tokens))
    if not conditional:
        return ""
    matched: list[str] = []
    for target, tokens in conditional:
        if any(
            token in value or value in token
            for token in tokens
            for value in values
        ):
            if target not in matched:
                matched.append(target)
    if len(matched) != 1:
        return ""
    # 冲突否决：槽位证据命中了一条边，但用户消息里逐字出现了**另一条**边
    # 的条件词元（如槽位残留「生产环境」而用户明说「普通权限」）——
    # 语义冲突时不做直判，交还 LLM 结合上下文裁决。
    message = " ".join(str(user_message or "").split()).strip().lower()
    if message:
        matched_targets = set(matched)
        for target, tokens in conditional:
            if target in matched_targets:
                continue
            if any(len(token) >= _MIN_TOKEN_CHARS and token in message for token in tokens):
                return ""
    return matched[0]


def _plan_tool_direct(
    requirement: Any,
    *,
    step: dict[str, Any],
    required_capabilities: list[str],
    required_slots: list[str],
    required_knowledge_ids: list[str],
    known_slots: dict[str, Any],
    trace_sink: Any,
) -> list[dict[str, Any]]:
    """场景 E：tool_call 节点按 input_schema 从槽位直组参数。

    required 能力在编译期已随 manifest 激活（initially_activated_names），
    直接产出 tool 动作即可调用，省掉 capability_describe + LLM 组参两轮。
    schema 必填参数无法全部由槽位满足时放弃（观测 skipped 原因）。
    """

    name = ""
    try:
        if required_slots or required_knowledge_ids:
            return []
        if str(step.get("type") or "").strip() != _TOOL_CALL_NODE_TYPE:
            return []
        tool_caps = [
            item for item in required_capabilities if item not in _INTERNAL_TOOL_NAMES
        ]
        if len(tool_caps) != 1:
            return []
        name = tool_caps[0]
        descriptor = None
        for item in getattr(requirement, "capability_manifest", None).available or []:
            if getattr(item, "name", "") == name and getattr(item, "available", False):
                descriptor = item
                break
        if descriptor is None:
            return []
        schema = getattr(descriptor, "input_schema", None)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if not isinstance(properties, dict) or not properties:
            return []
        schema_required = schema.get("required")
        schema_required = (
            [str(item) for item in schema_required]
            if isinstance(schema_required, list)
            else []
        )
        arguments = {
            str(prop): value
            for prop, value in known_slots.items()
            if prop in properties and value not in (None, "", [], {})
        }
        missing = [prop for prop in schema_required if prop not in arguments]
        if missing:
            return _emit(
                trace_sink,
                "tool_direct_call_skipped",
                [],
                tool_name=name,
                missing_required=missing,
            )
        step_name = str(step.get("name") or "").strip()
        action = {
            "action": "tool",
            "tool_name": name,
            "arguments": arguments,
            "task_summary": (
                f"SOP 工具节点「{step_name}」按槽位直组参数调用 {name}（跳过 LLM 决策轮）。"
            ),
        }
        return _emit(
            trace_sink,
            "tool_direct_call",
            [action],
            tool_name=name,
            argument_keys=sorted(arguments),
        )
    except Exception:  # noqa: BLE001 - 执行器绝不阻断主链路
        return []


EXTRACTION_SYSTEM_PROMPT = (
    "你是工单槽位抽取器。任务：从用户消息中为指定字段提取取值。"
    "字段名是 snake_case 抽象标识，field_context 是当前步骤的业务说明，"
    "用它理解每个字段收集的是什么信息。"
    "规则：1) 只提取用户消息中明确表达的信息，严禁编造或推测；"
    "2) 一段口语描述可以拆开映射到多个字段"
    "（如「生产环境的管理员权限」可拆为 权限级别=管理员、环境/访问级别=生产环境）；"
    "3) 某字段在消息中没有明确取值就省略该字段；"
    "4) 取值沿用用户消息中的原始词汇，不要翻译或改写"
    "（如「普通权限」就写 普通权限，不要归一化成 normal）。"
    "只输出 JSON 对象，形如 {'slot_values': {'字段名': '取值'}}。"
)


def _extract_slots_llm(
    fields: list[str],
    user_message: str,
    known_slots: dict[str, Any],
    model_config: Any,
    trace_sink: Any,
    *,
    step_instruction: str = "",
) -> dict[str, Any]:
    """轻量槽位抽取：从用户首条消息中提取期望字段值。

    P2 核心：原 task_action 链路里这层抽取混在 25-110s 的大 payload 决策轮，
    这里拆成独立的 1-3s 轻量调用。失败/不可用时返回空 dict，调用方退回
    模板询问（保守行为）。step_instruction 是 SOP 节点说明，为抽象字段名
    提供业务语义（如 access_level=访问级别/环境类型）。
    """

    if model_config is None or not fields or not user_message.strip():
        return {}
    import time as _time

    started = _time.monotonic()
    result: dict[str, Any] = {}
    try:
        from app.llm import LLMClient
        from app.observability.spans import llm_operation

        client = LLMClient(model_config)
        payload = {
            "fields": list(fields),
            "field_context": str(step_instruction or "")[:600],
            "known_slots": known_slots,
            "user_message": user_message[:2_000],
        }
        # 专用 operation：output_policy 据此收紧 json 修复/空响应重试
        # （默认 3 次修复在慢模型上会串行烧 2-4 轮完整 LLM 调用）
        with llm_operation("sop.slot_extraction"):
            raw = client.generate_json(EXTRACTION_SYSTEM_PROMPT, payload)
        values = raw.get("slot_values") if isinstance(raw, dict) else None
        result = dict(values) if isinstance(values, dict) else {}
    except Exception:  # noqa: BLE001 - 抽取失败退回模板询问，绝不阻断
        result = {}
    if callable(trace_sink):
        try:
            trace_sink(
                "sop_slot_extraction",
                {
                    "fields": list(fields),
                    "extracted": result,
                    "duration_ms": round((_time.monotonic() - started) * 1000, 1),
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return result


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


def _plan_handoff_passthrough(
    step: dict[str, Any],
    known_slots: dict[str, Any],
    trace_sink: Any,
    *,
    allowed_fields: set[str] | None = None,
) -> list[dict[str, Any]]:
    """场景 F：handoff 终点节点直通，零 LLM 发起转人工。

    status 直接置 handoff（引擎据 result.status 建人工请求并挂起会话）；
    回复模板带槽位摘要，只展示技能声明字段（slot_fields）——历史遗留
    的脏槽位（如上一轮的 permission_preference）不会出现在用户回复里。
    """
    allowed = {str(item).strip() for item in (allowed_fields or set()) if str(item).strip()}
    filled = {
        str(key): value
        for key, value in (known_slots or {}).items()
        if (not allowed or str(key).strip() in allowed)
        and value not in (None, "", [], {})
    }
    summary = "；".join(f"{key}：{value}" for key, value in filled.items())
    reply = (
        f"已根据您提供的信息（{summary}）发起人工处理，会有工作人员跟进。"
        if summary
        else "已发起人工处理，会有工作人员跟进，请稍候。"
    )
    step_name = str(step.get("name") or step.get("node_id") or "").strip()
    action = {
        "action": "finish",
        "status": "handoff",
        "reply_fragment": reply,
        "task_summary": f"SOP 步骤「{step_name}」为人工处理终点，确定性发起转人工。",
        "slot_updates": {},
    }
    return _emit(
        trace_sink,
        "handoff_passthrough",
        [action],
        known_slots_count=len(filled),
    )


def _await_user_action(step: dict[str, Any], required_slots: list[str]) -> dict[str, Any]:
    step_name = str(step.get("name") or "").strip()
    # 只问调用方传入的缺失字段（expected_user_info 里可能有些字段已满足，
    # 全量罗列会重现「已提供还要求补全」的错误询问）
    missing = [str(item).strip() for item in required_slots if str(item).strip()]
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
