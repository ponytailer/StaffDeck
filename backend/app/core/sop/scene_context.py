"""SOP 场景上下文（SceneContext）。

批次 4.4 自 ``sop/scenes.py`` 搬出，零行为改动。``scenes.py`` 保持 re-export；
``_extract_fresh_slots`` 内对 ``_extract_slots_llm`` 的引用走方法内 lazy import，
既避免 scenes ↔ scene_context 循环，又保住测试对 ``scenes_module._extract_slots_llm``
的 monkeypatch 路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.sop.edge_eval import (
    _EdgeCondition,
    _evaluate_edges,
    _last_result_ok,
)

@dataclass
class SceneContext:
    """一次 plan 调用的共享上下文。

    轻量视图（槽位/能力/出边声明）直接从 requirement 派生；涉及 LLM 的重
    推导（A2 新鲜抽取）与派生视图（``merged_slots`` / ``edge_conditions``）
    做成**惰性 memoize**——只有 F/D/G/B 场景会触碰，保证 C/E/A 命中时不会
    多出一次抽取 LLM 调用（与改造前阶梯的执行位置严格对齐）。
    """

    requirement: Any
    step: dict[str, Any]
    sop_context: dict[str, Any]
    resume_slot_recovery: bool
    satisfied_required_knowledge_ids: set[str] | None
    trace_sink: Any
    slot_extraction_model: Any
    edge_condition_specs: Mapping[str, Any] | None
    slot_submission: Mapping[str, Any] | None
    # 场景 L：Laya 出边决策旁路（未启用/影子模式时为 None 或 enabled=False）
    laya_router: Any = None

    _fresh_slots: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _merged_slots: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _edge_conditions: list[_EdgeCondition] | None = field(
        default=None, init=False, repr=False
    )

    # -- 轻量视图（纯派生，无副作用） -------------------------------------

    @property
    def slot_fields(self) -> list[str]:
        # 技能全图声明的槽位字段（编译期从各节点 expected_user_info 并集而来），
        # 作为路由证据/回复摘要的字段白名单，隔离会话历史遗留的脏槽位。
        return [
            str(item).strip()
            for item in (self.sop_context.get("slot_fields") or [])
            if str(item).strip()
        ]

    @property
    def slot_labels(self) -> Mapping[str, Any]:
        # 字段显示名（技能自报，内置词典兜底）——只用于拼**用户可见回复**，
        # 不参与任何判定。缺省时全部走内置词典 / 原样返回。
        declared = self.sop_context.get("slot_labels")
        return declared if isinstance(declared, Mapping) else {}

    @property
    def required_slots(self) -> list[str]:
        return [str(item) for item in getattr(self.requirement, "required_slots", []) or []]

    @property
    def required_capabilities(self) -> list[str]:
        return [
            str(item)
            for item in getattr(self.requirement, "required_capability_names", []) or []
        ]

    @property
    def required_knowledge_ids(self) -> list[str]:
        return [
            str(item)
            for item in getattr(self.requirement, "required_knowledge_base_ids", []) or []
        ]

    @property
    def known_slots(self) -> dict[str, Any]:
        return dict(getattr(self.requirement, "known_slots", {}) or {})

    @property
    def source_message(self) -> str:
        return str(getattr(self.requirement, "source_user_message", "") or "")

    @property
    def allowed_transitions(self) -> list[Any]:
        return list(getattr(self.requirement, "allowed_transitions", []) or [])

    @property
    def step_type(self) -> str:
        return str(self.step.get("type") or "").strip()

    @property
    def skill_id(self) -> str:
        return str(self.sop_context.get("skill_id") or "")

    # -- 惰性重推导（F/D/G/B 专属，含一次轻量抽取 LLM） -------------------

    @property
    def fresh_slots(self) -> dict[str, Any]:
        if self._fresh_slots is None:
            self._fresh_slots = self._extract_fresh_slots()
        return self._fresh_slots

    def _extract_fresh_slots(self) -> dict[str, Any]:
        # 循环引用规避：scenes → scene_context（模块级），此处反向按需取
        from app.core.sop.scenes import _extract_slots_llm

        # 场景 A2：会话复用的新鲜度保护。required_slots 为空只说明
        # session.slots_json 里有值——可能是**上一轮请求**的旧值（同会话
        # 第二次申请换了工号，17:41 复测踩中：旧工号1003 带着走完全程）。
        # 当前节点声明了 expected_user_info 且有新用户消息时跑一次轻量抽取，
        # 新值合并进路由证据（merged_slots）并随 B 直通的 slot_updates 落库
        # 覆盖旧值。
        if (
            self.required_slots
            or self.resume_slot_recovery
            or self.slot_extraction_model is None
        ):
            return {}
        fresh_expected = [
            str(item).strip()
            for item in (self.step.get("expected_user_info") or [])
            if str(item).strip()
        ]
        if not fresh_expected or not self.source_message.strip():
            return {}
        extracted = _extract_slots_llm(
            fresh_expected,
            self.source_message,
            self.known_slots,
            self.slot_extraction_model,
            self.trace_sink,
            step_instruction=str(self.step.get("instruction") or ""),
            mode="fresh_merge",
        )
        return {
            str(key): value
            for key, value in extracted.items()
            if str(key) in set(fresh_expected) and value not in (None, "", [], {})
        }

    @property
    def merged_slots(self) -> dict[str, Any]:
        if self._merged_slots is None:
            fresh = self.fresh_slots
            self._merged_slots = (
                {**self.known_slots, **fresh} if fresh else self.known_slots
            )
        return self._merged_slots

    @property
    def edge_conditions(self) -> list[_EdgeCondition]:
        """出边的结构化求值（场景 G 的证据）：一次算好，供 F/D/G/B 复用。

        找不到任何编译产物时 spec 全为 None，等价于改造前的行为。
        """
        if self._edge_conditions is None:
            self._edge_conditions = _evaluate_edges(
                self.allowed_transitions,
                step=self.step,
                specs=self.edge_condition_specs,
                slots=self.merged_slots,
                user_message=self.source_message,
                allowed_fields=set(self.slot_fields),
                last_result_ok=_last_result_ok(self.requirement),
            )
        return self._edge_conditions


