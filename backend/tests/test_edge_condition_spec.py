"""SOP 边条件结构化契约 + 求值器的单元测试。

这一层是纯函数，测试重点是**保守性**：拿不准就必须返回 ``None``（交还 LLM），
绝不能把「不知道」当成「不命中」——多边路由里一个错误的 False 会把流程带错分支。
"""

from __future__ import annotations

import pytest

from app.skills.edge_condition_spec import (
    COMPILER_VERSION,
    EdgeConditionSpec,
    EdgeEvalContext,
    SlotComparison,
    always_spec,
    condition_fingerprint,
    evaluate_edge_condition,
    is_unconditional,
    slot_has_value,
    spec_from_condition_text,
    spec_from_payload,
)


# ---------------------------------------------------------------------------
# 预设文本直译
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("上一步工具调用成功后进入", "result_ok"),
        ("上一步工具调用失败后进入", "result_failed"),
        ("用户明确确认后进入", "user_confirmed"),
        ("用户明确拒绝后进入", "user_rejected"),
        ("还有必填信息没有收集到时进入", "slots_missing"),
        ("缺少某个指定字段时进入", "slots_missing"),
        ("所有必填信息都收集完成后进入", "slots_all"),
    ],
)
def test_frontend_preset_texts_are_translated(text: str, kind: str) -> None:
    """前端 CONDITION_PRESET_TEXT 落库的中文必须能被逐字还原成结构化条件。"""

    spec = spec_from_condition_text(text)
    assert spec is not None
    assert spec.kind == kind
    assert spec.source == "preset"


@pytest.mark.parametrize("text", ["", "always", "true", "default", "else", "   "])
def test_unconditional_writings_map_to_always(text: str) -> None:
    spec = spec_from_condition_text(text)
    assert spec is not None
    assert spec.kind == "always"
    assert is_unconditional(text)


@pytest.mark.parametrize(
    "text",
    [
        "如果客户是 VIP 且余额大于 1000 就进入",
        "需要外部商品数据时进入",
        "审批未通过时进入",
        "视情况而定",
    ],
)
def test_free_text_is_not_translated_deterministically(text: str) -> None:
    """没有预设可对上的自然语言一律交 LLM 编译——绝不猜。"""

    assert spec_from_condition_text(text) is None


def test_preset_substring_match_requires_long_text() -> None:
    """带外层修饰的已知预设文本仍能认出（长串容错）。"""

    spec = spec_from_condition_text("用户点击确认按钮后，用户明确确认后进入")
    assert spec is not None
    assert spec.kind == "user_confirmed"


def test_spec_from_payload_rejects_invalid_payload() -> None:
    assert spec_from_payload({"kind": "not_a_kind"}) is None
    assert spec_from_payload("not a dict") is None
    assert spec_from_payload(None) is None
    spec = spec_from_payload({"kind": "slots_all", "fields": ["工号"]})
    assert spec is not None
    assert spec.fields == ["工号"]


def test_spec_from_payload_roundtrips_model() -> None:
    original = EdgeConditionSpec(kind="slot_compare", comparisons=[SlotComparison(field="a", op="eq", value="b")])
    assert spec_from_payload(original) is original
    assert spec_from_payload(original.model_dump(mode="json")) == original


# ---------------------------------------------------------------------------
# 槽位类条件
# ---------------------------------------------------------------------------


def test_slots_all_and_any_and_missing() -> None:
    ctx = EdgeEvalContext(slots={"a": "1", "b": ""}, node_required_fields=("a", "b"))
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_all", fields=["a", "b"]), ctx) is False
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_all", fields=["a"]), ctx) is True
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_any", fields=["a", "b"]), ctx) is True
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_missing", fields=["a", "b"]), ctx) is True
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_missing", fields=["a"]), ctx) is False


def test_empty_fields_fall_back_to_node_required_fields() -> None:
    spec = EdgeConditionSpec(kind="slots_missing")
    ctx = EdgeEvalContext(slots={"a": "1"}, node_required_fields=("a", "b"))
    assert evaluate_edge_condition(spec, ctx) is True
    ctx_full = EdgeEvalContext(slots={"a": "1", "b": "2"}, node_required_fields=("a", "b"))
    assert evaluate_edge_condition(spec, ctx_full) is False


def test_empty_fields_without_node_scope_is_unknown() -> None:
    """字段无从解析时必须返回 None，而不是「没有缺失」。"""

    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_missing"), EdgeEvalContext(slots={})) is None
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slots_all"), EdgeEvalContext(slots={})) is None


def test_slots_missing_ignores_dirty_slots_outside_whitelist() -> None:
    """非技能声明字段不参与判定（调用方已按白名单过滤，这里验证字段不存在即缺）。"""

    spec = EdgeConditionSpec(kind="slots_all", fields=["permission_level"])
    ctx = EdgeEvalContext(slots={"permission_preference": "管理员"}, node_required_fields=())
    assert evaluate_edge_condition(spec, ctx) is False


# ---------------------------------------------------------------------------
# 值比较
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "expected", "slot", "result"),
    [
        ("eq", "管理员", "管理员", True),
        ("eq", "管理员", "普通", False),
        ("ne", "管理员", "普通", True),
        ("contains", "管理员", "生产环境的管理员权限", True),
        ("contains", "生产环境的管理员权限", "管理员", True),
        ("in", ["a", "b"], "b", True),
        ("gte", 5000, "12000", True),
        ("lte", 5000, "300", True),
        ("gte", 5000, "300", False),
    ],
)
def test_slot_compare_ops(op: str, expected: object, slot: str, result: bool) -> None:
    spec = EdgeConditionSpec(
        kind="slot_compare",
        comparisons=[SlotComparison(field="f", op=op, value=expected)],
    )
    assert evaluate_edge_condition(spec, EdgeEvalContext(slots={"f": slot})) is result


def test_slot_compare_unknown_when_slot_empty_or_unparsable() -> None:
    numeric = EdgeConditionSpec(kind="slot_compare", comparisons=[SlotComparison(field="f", op="gte", value=10)])
    assert evaluate_edge_condition(numeric, EdgeEvalContext(slots={})) is None
    assert evaluate_edge_condition(numeric, EdgeEvalContext(slots={"f": "很多"})) is None


def test_slot_compare_without_comparisons_is_unknown() -> None:
    assert evaluate_edge_condition(EdgeConditionSpec(kind="slot_compare"), EdgeEvalContext(slots={})) is None


# ---------------------------------------------------------------------------
# 用户意图 / 上一步结果
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["好的，确认提交", "我同意", "可以", "没问题"])
def test_user_confirmed_markers(message: str) -> None:
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_confirmed"), EdgeEvalContext(slots={}, user_message=message)) is True


@pytest.mark.parametrize("message", ["我不同意", "不用了谢谢", "取消吧", "算了"])
def test_user_rejected_markers(message: str) -> None:
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_rejected"), EdgeEvalContext(slots={}, user_message=message)) is True


@pytest.mark.parametrize("message", ["我再看看", "可能吧"])
def test_ambiguous_user_message_is_not_confirmation(message: str) -> None:
    """有消息但没出现确认词 → 明确「未确认」（可以安全地走另一条分支）。"""

    ctx = EdgeEvalContext(slots={}, user_message=message)
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_confirmed"), ctx) is False
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_rejected"), ctx) is False


def test_empty_user_message_makes_intent_conditions_unknown() -> None:
    """连用户消息都没有时「未确认」也判不了——必须交还 LLM。"""

    ctx = EdgeEvalContext(slots={}, user_message="   ")
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_confirmed"), ctx) is None
    assert evaluate_edge_condition(EdgeConditionSpec(kind="user_rejected"), ctx) is None


def test_result_ok_and_failed_require_a_result() -> None:
    ok = EdgeConditionSpec(kind="result_ok")
    failed = EdgeConditionSpec(kind="result_failed")
    assert evaluate_edge_condition(ok, EdgeEvalContext(slots={}, last_result_ok=None)) is None
    assert evaluate_edge_condition(failed, EdgeEvalContext(slots={}, last_result_ok=None)) is None
    assert evaluate_edge_condition(ok, EdgeEvalContext(slots={}, last_result_ok=True)) is True
    assert evaluate_edge_condition(failed, EdgeEvalContext(slots={}, last_result_ok=True)) is False
    assert evaluate_edge_condition(ok, EdgeEvalContext(slots={}, last_result_ok=False)) is False
    assert evaluate_edge_condition(failed, EdgeEvalContext(slots={}, last_result_ok=False)) is True


# ---------------------------------------------------------------------------
# 契约元信息
# ---------------------------------------------------------------------------


def test_llm_judge_always_defers_to_llm() -> None:
    spec = EdgeConditionSpec(kind="llm_judge")
    assert spec.is_deterministic() is False
    assert evaluate_edge_condition(spec, EdgeEvalContext(slots={"a": 1})) is None


def test_always_is_deterministic_true() -> None:
    spec = always_spec()
    assert spec.kind == "always"
    assert evaluate_edge_condition(spec, EdgeEvalContext(slots={})) is True


def test_none_spec_is_unknown() -> None:
    assert evaluate_edge_condition(None, EdgeEvalContext(slots={})) is None


def test_readable_text_is_human_reviewable() -> None:
    assert "总是进入" in always_spec().readable()
    spec = EdgeConditionSpec(kind="slots_missing", fields=["工号"])
    assert spec.readable() == "指定字段存在缺失：工号"
    compare = EdgeConditionSpec(
        kind="slot_compare", comparisons=[SlotComparison(field="金额", op="gte", value=5000)]
    )
    assert "金额 gte 5000" in compare.readable()


def test_compiler_version_is_stamped() -> None:
    assert EdgeConditionSpec(kind="always").compiler_version == COMPILER_VERSION


def test_slot_has_value_semantics() -> None:
    assert slot_has_value("x") is True
    assert slot_has_value("  ") is False
    assert slot_has_value(0) is True
    assert slot_has_value(False) is True
    assert slot_has_value(None) is False
    assert slot_has_value([]) is False
    assert slot_has_value({}) is False
    assert slot_has_value([1]) is True


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def test_fingerprint_changes_with_any_material_input() -> None:
    base = condition_fingerprint("n1", "n2", "条件", ["a"])
    assert base == condition_fingerprint("n1", "n2", "条件", ["a"])
    assert base != condition_fingerprint("n1", "n2", "别的条件", ["a"])
    assert base != condition_fingerprint("n1", "n3", "条件", ["a"])
    assert base != condition_fingerprint("n9", "n2", "条件", ["a"])
    # 源节点必填字段变化 → 指纹变化：slots_missing 的默认作用域变了，
    # 旧编译结果必须失效
    assert base != condition_fingerprint("n1", "n2", "条件", ["a", "b"])
    assert base != condition_fingerprint("n1", "n2", "条件", ["a", "a", "b"])


def test_fingerprint_normalizes_whitespace() -> None:
    assert condition_fingerprint("n1", "n2", " 条件  A ", []) == condition_fingerprint(
        "n1", "n2", "条件 A", []
    )
