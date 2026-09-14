"""SOP 边条件结构化契约 + 纯函数求值器。

背景（为什么要这层契约）
------------------------
``SkillGraphEdge.condition`` 目前只有**自由文本**一种持久化形态。前端
``DistillPage.tsx`` 的 ``CONDITION_PRESET_OPTIONS`` 本来已提供结构化预设
（``tool_success`` / ``tool_failed`` / ``user_confirmed`` / ``missing_slots([])``
/ ``all_required_info_collected`` …），但 ``conditionFromPreset()`` 在写入时就把
标识翻译成了中文（``tool_success`` → 「上一步工具调用成功后进入」），**语义当场
丢失**。后端拿到的只是一句中文，每次 SOP 推进都得让主模型重新理解一遍。

本模块定义 ``EdgeConditionSpec``：边条件的**结构化契约**。

- 阶段① 本模块：契约 + 预设文本的确定性映射 + 纯函数求值器；
- 阶段② ``edge_condition_compiler.py``：把自然语言一次性编译成 spec 并落库；
- 阶段③ ``sop_step_executor`` 场景 G：用 spec + 槽位/上一步结果求值，
  命中唯一即直判，零 LLM。

``llm_judge`` 表示「确实需要语义判断」。它的求值恒返回 ``None``，调用方回落
LLM——**本次刻意不做激进边界判定**，只把这类边标记清楚，便于后续单独推进。

设计约束
--------
- 纯函数、无 IO、无 LLM：只有这样才能在运行时的热路径里零成本调用。
- 求值结果三态：``True`` 命中 / ``False`` 明确不命中 / ``None`` 未知（交还
  LLM）。**未知绝不等价于不命中**——多边路由里只要有一条「未知且互斥」的边，
  就不能直判。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 契约
# ---------------------------------------------------------------------------

ConditionKind = Literal[
    "always",  # 无条件（兜底 / else）
    "slots_all",  # 指定字段全部有值
    "slots_any",  # 指定字段任一有值
    "slots_missing",  # 指定字段至少一个缺值（fields 为空 → 当前节点声明的必填字段）
    "slot_compare",  # 槽位取值与期望值比较
    "user_confirmed",  # 用户在本轮明确确认
    "user_rejected",  # 用户在本轮明确拒绝
    "result_ok",  # 上一步能力/检索调用成功
    "result_failed",  # 上一步能力/检索调用失败
    "llm_judge",  # 仍需语义判断 → 求值恒 None，交还 LLM
]

CompareOp = Literal["eq", "ne", "contains", "in", "gte", "lte"]

# 编译器版本：契约语义变更时递增，便于一次性识别历史编译结果是否需要重编
COMPILER_VERSION = "1"

# 供人工复核时的可读标签
KIND_LABELS: dict[str, str] = {
    "always": "总是进入",
    "slots_all": "指定字段全部已收集",
    "slots_any": "指定字段任一已收集",
    "slots_missing": "指定字段存在缺失",
    "slot_compare": "槽位取值满足比较",
    "user_confirmed": "用户已确认",
    "user_rejected": "用户已拒绝",
    "result_ok": "上一步调用成功",
    "result_failed": "上一步调用失败",
    "llm_judge": "需要模型语义判断",
}


class SlotComparison(BaseModel):
    """单个槽位的取值比较。"""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: CompareOp = "contains"
    value: str | float | list[str] | bool


class EdgeConditionSpec(BaseModel):
    """一条边条件的结构化契约。"""

    model_config = ConfigDict(extra="forbid")

    kind: ConditionKind = "llm_judge"
    # slots_all / slots_any / slots_missing 的作用字段；空列表语义见文档
    fields: list[str] = Field(default_factory=list)
    # slot_compare 的比较列表（多条之间是「且」）
    comparisons: list[SlotComparison] = Field(default_factory=list)
    # 仅用于人工复核，不参与运行时判定
    confidence: float = 1.0
    rationale: str = ""
    source: Literal["preset", "llm", "manual"] = "llm"
    compiler_version: str = COMPILER_VERSION

    def is_deterministic(self) -> bool:
        """是否可脱离 LLM 求值。"""

        return self.kind != "llm_judge"

    def readable(self) -> str:
        """人工复核用的中文描述。"""

        label = KIND_LABELS.get(self.kind, self.kind)
        if self.kind in {"slots_all", "slots_any", "slots_missing"} and self.fields:
            return f"{label}：{'、'.join(self.fields)}"
        if self.kind == "slot_compare" and self.comparisons:
            parts = [f"{item.field} {item.op} {item.value}" for item in self.comparisons]
            return f"{label}：{'；'.join(parts)}"
        return label


def always_spec(source: Literal["preset", "llm", "manual"] = "manual") -> EdgeConditionSpec:
    return EdgeConditionSpec(
        kind="always",
        source=source,
        confidence=1.0,
        rationale="无条件边（default / else / 总是可进入）。",
    )


# ---------------------------------------------------------------------------
# 预设文本 → spec 的确定性映射
# ---------------------------------------------------------------------------

# 与前端 CONDITION_PRESET_TEXT 逐字一致（DistillPage.tsx）。
# 前端把结构化标识翻译成这些中文后才落库，这里做反向还原。
PRESET_CONDITION_TEXT: dict[str, str] = {
    "missing_required_info": "还有必填信息没有收集到时进入",
    "missing_slots([])": "缺少某个指定字段时进入",
    "all_required_info_collected": "所有必填信息都收集完成后进入",
    "tool_success": "上一步工具调用成功后进入",
    "tool_failed": "上一步工具调用失败后进入",
    "user_confirmed": "用户明确确认后进入",
    "user_rejected": "用户明确拒绝后进入",
}

_PRESET_TEXT_TO_SPEC: dict[str, EdgeConditionSpec] = {
    "missing_required_info": EdgeConditionSpec(
        kind="slots_missing",
        confidence=1.0,
        rationale="前端预设 missing_required_info：当前节点声明的必填字段任一缺失。",
    ),
    # 前端把字段列表丢在 `missing_slots([])` 里（页面只让填字段名却写成空数组），
    # 落库文本无法恢复具体字段 → 退化为「当前节点声明的必填字段任一缺失」。
    # 语义与用户当时的意图一致，只是范围可能偏大；标记低置信度供人工复核。
    "missing_slots([])": EdgeConditionSpec(
        kind="slots_missing",
        confidence=0.6,
        rationale="前端预设 missing_slots([])：具体字段名未随条件落库，退化为当前节点必填字段任一缺失。",
    ),
    "all_required_info_collected": EdgeConditionSpec(
        kind="slots_all",
        confidence=1.0,
        rationale="前端预设 all_required_info_collected：当前节点声明的必填字段全部已收集。",
    ),
    "tool_success": EdgeConditionSpec(
        kind="result_ok",
        confidence=1.0,
        rationale="前端预设 tool_success：上一步能力调用成功。",
    ),
    "tool_failed": EdgeConditionSpec(
        kind="result_failed",
        confidence=1.0,
        rationale="前端预设 tool_failed：上一步能力调用失败。",
    ),
    "user_confirmed": EdgeConditionSpec(
        kind="user_confirmed",
        confidence=1.0,
        rationale="前端预设 user_confirmed：用户本轮明确确认。",
    ),
    "user_rejected": EdgeConditionSpec(
        kind="user_rejected",
        confidence=1.0,
        rationale="前端预设 user_rejected：用户本轮明确拒绝。",
    ),
}

# 无条件边的历史写法（与 GraphRules.edge_condition 的空值语义一致）
_UNCONDITIONAL_TOKENS = {"", "always", "true", "default", "else", "无条件", "默认"}

# 实际落库的是**中文**（conditionFromPreset 的产物）；同时兼容直接落库标识的链路。
# 键 = 条件原文，值 = spec。
_CONDITION_TEXT_TO_SPEC: dict[str, EdgeConditionSpec] = {}
for _preset_key, _preset_text in PRESET_CONDITION_TEXT.items():
    _CONDITION_TEXT_TO_SPEC[_preset_text] = _PRESET_TEXT_TO_SPEC[_preset_key]
# 原始标识本身也认（历史/接口直写的场景）
for _preset_key, _preset_spec in _PRESET_TEXT_TO_SPEC.items():
    _CONDITION_TEXT_TO_SPEC.setdefault(_preset_key, _preset_spec)


def is_unconditional(condition: Any) -> bool:
    """判断一条边的条件文本是否为「无条件」。"""

    return str(condition or "").strip().lower() in _UNCONDITIONAL_TOKENS


def spec_from_condition_text(condition: Any) -> EdgeConditionSpec | None:
    """确定性识别：能直译就返回 spec，识别不了返回 ``None``（交 LLM 编译）。

    只做**逐字匹配**——不做任何模糊/语义猜测。宁可交 LLM 编译，也不误判一条
    带业务语义的条件边。
    """

    text = " ".join(str(condition or "").split()).strip()
    if is_unconditional(condition):
        return always_spec(source="preset")
    if not text:
        return None
    preset = _CONDITION_TEXT_TO_SPEC.get(text)
    if preset is not None:
        return preset.model_copy(update={"source": "preset"})
    # 容错：条件被包了外层修饰（如「用户点击确认后，用户明确确认后进入」）时也能认出；
    # 只匹配长文本，避免「用户确认」这类短串误配到不相关的条件上。
    for known_text, spec in _CONDITION_TEXT_TO_SPEC.items():
        if len(known_text) < 8:
            continue
        if known_text in text:
            return spec.model_copy(update={"source": "preset"})
    return None


def spec_from_payload(payload: Any) -> EdgeConditionSpec | None:
    """把编译器/人工写入的 JSON 载荷解析成 spec；非法返回 ``None``。"""

    if isinstance(payload, EdgeConditionSpec):
        return payload
    if not isinstance(payload, dict):
        return None
    try:
        return EdgeConditionSpec.model_validate(payload)
    except Exception:  # noqa: BLE001 - 契约不合法一律视为未编译
        return None


# ---------------------------------------------------------------------------
# 求值
# ---------------------------------------------------------------------------

# 确认/拒绝的确定性词表：**保守**。只要出现明确确认/拒绝表述才判定，
# 模糊表述（「再看看」「可能吧」）一律返回 None 交还 LLM。
CONFIRM_MARKERS = ("确认", "确定", "同意", "可以", "好的", "没问题", "是的", "提交", "就这样", "批准")
REJECT_MARKERS = ("拒绝", "不用了", "不需要", "取消", "不要", "算了", "不同意", "驳回", "作废")


@dataclass(frozen=True)
class EdgeEvalContext:
    """求值所需的全部证据（都由调用方从已有数据里取，无额外 IO）。"""

    slots: Mapping[str, Any]
    # 当前节点声明的必填字段（expected_user_info）——slots_missing/all 的默认作用域
    node_required_fields: tuple[str, ...] = ()
    user_message: str = ""
    # 上一步能力/检索调用的结果；None = 本轮没有可用的调用结果
    last_result_ok: bool | None = None


def slot_has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _scoped_fields(spec: EdgeConditionSpec, ctx: EdgeEvalContext) -> list[str]:
    fields = [str(item).strip() for item in spec.fields if str(item).strip()]
    if fields:
        return fields
    return [str(item).strip() for item in ctx.node_required_fields if str(item).strip()]


def evaluate_edge_condition(
    spec: EdgeConditionSpec | None,
    ctx: EdgeEvalContext,
) -> bool | None:
    """对一条边条件求值：``True`` / ``False`` / ``None``（未知）。

    调用约定：``None`` 表示「这条边无法判定，不能用它做直判」——多边路由里
    只要出现一个 ``None``，调用方就必须回落 LLM。
    """

    if spec is None:
        return None
    kind = spec.kind
    if kind == "llm_judge":
        return None
    if kind == "always":
        return True

    if kind == "slots_all":
        fields = _scoped_fields(spec, ctx)
        if not fields:
            return None
        return all(slot_has_value(ctx.slots.get(field)) for field in fields)

    if kind == "slots_any":
        fields = _scoped_fields(spec, ctx)
        if not fields:
            return None
        return any(slot_has_value(ctx.slots.get(field)) for field in fields)

    if kind == "slots_missing":
        fields = _scoped_fields(spec, ctx)
        if not fields:
            return None
        return any(not slot_has_value(ctx.slots.get(field)) for field in fields)

    if kind == "slot_compare":
        if not spec.comparisons:
            return None
        results = [_compare(ctx.slots.get(item.field), item) for item in spec.comparisons]
        if any(item is None for item in results):
            return None
        return all(bool(item) for item in results)

    if kind == "user_confirmed":
        return _match_user_intent(ctx.user_message, CONFIRM_MARKERS)

    if kind == "user_rejected":
        return _match_user_intent(ctx.user_message, REJECT_MARKERS)

    if kind == "result_ok":
        return None if ctx.last_result_ok is None else bool(ctx.last_result_ok)

    if kind == "result_failed":
        return None if ctx.last_result_ok is None else not bool(ctx.last_result_ok)

    return None


def _match_user_intent(message: str, markers: Sequence[str]) -> bool | None:
    text = " ".join(str(message or "").split()).strip()
    if not text:
        return None
    if any(marker in text for marker in markers):
        return True
    return False


def _compare(slot_value: Any, comparison: SlotComparison) -> bool | None:
    if not slot_has_value(slot_value):
        return None
    actual = slot_value
    expected = comparison.value
    op = comparison.op

    if op in {"eq", "ne"}:
        equal = _loose_equal(actual, expected)
        return equal if op == "eq" else not equal
    if op == "contains":
        actual_text = str(actual).strip().lower()
        expected_text = str(expected).strip().lower()
        if not actual_text or not expected_text:
            return None
        return expected_text in actual_text or actual_text in expected_text
    if op == "in":
        candidates = expected if isinstance(expected, list) else [expected]
        actual_text = str(actual).strip().lower()
        return any(str(item).strip().lower() == actual_text for item in candidates)
    if op in {"gte", "lte"}:
        actual_number = _to_number(actual)
        expected_number = _to_number(expected)
        if actual_number is None or expected_number is None:
            return None
        return actual_number >= expected_number if op == "gte" else actual_number <= expected_number
    return None


def _loose_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return _to_bool(actual) == _to_bool(expected)
    actual_number = _to_number(actual)
    expected_number = _to_number(expected)
    if actual_number is not None and expected_number is not None:
        return actual_number == expected_number
    return str(actual).strip().lower() == str(expected).strip().lower()


def _to_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group(0)) if match else None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "是", "已确认", "已同意"}


# ---------------------------------------------------------------------------
# 指纹：编译结果的失效依据
# ---------------------------------------------------------------------------


def condition_fingerprint(
    source_node_id: str,
    next_node_id: str,
    condition: Any,
    node_required_fields: Sequence[str] | None = None,
) -> str:
    """边条件指纹。

    结构化编译结果**不写回 content_json**（前端 SkillCard 类型里没有这个字段，
    来回一趟就会被丢掉），而是按指纹存进 ``skill_edge_conditions`` 表。指纹
    覆盖「边端点 + 条件原文 + 源节点必填字段」——任何一项变了指纹就变，旧编译
    结果自然失效，不需要任何显式清理。
    """

    material = "\x1f".join(
        [
            str(source_node_id or "").strip(),
            str(next_node_id or "").strip(),
            " ".join(str(condition or "").split()).strip(),
            ",".join(str(item).strip() for item in (node_required_fields or ())),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "COMPILER_VERSION",
    "CONFIRM_MARKERS",
    "ConditionKind",
    "EdgeConditionSpec",
    "EdgeEvalContext",
    "KIND_LABELS",
    "PRESET_CONDITION_TEXT",
    "REJECT_MARKERS",
    "SlotComparison",
    "always_spec",
    "condition_fingerprint",
    "evaluate_edge_condition",
    "is_unconditional",
    "slot_has_value",
    "spec_from_condition_text",
    "spec_from_payload",
]
