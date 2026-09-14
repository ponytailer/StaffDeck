"""SOP 边条件离线编译器（阶段②）。

职责：把 ``SkillGraphEdge.condition`` 的自然语言**一次性**编译成
``EdgeConditionSpec``（结构化契约），落进 ``skill_edge_conditions`` 表。
运行时（``sop_step_executor`` 场景 G）按指纹取用，命中唯一即直判，零 LLM。

「编译一次、跑无数次」——编译只在技能保存 / 发布 / 历史回填时发生（异步，
见 ``edge_condition_jobs``），对话链路只做纯函数求值。

两级策略
--------
1. **预设直译**（零 LLM）：前端 ``CONDITION_PRESET_OPTIONS`` 落库的中文与
   ``edge_condition_spec.spec_from_condition_text`` 逐字匹配即出 spec。
   实测 25 张图 91 条条件边里，这类占相当比例，且成本为零。
2. **LLM 批量编译**：一次调用处理整张图剩余的条件边（附带节点上下文：
   名称/说明/必填字段/允许动作），输出 JSON spec 列表。失败保持自由文本
   （``status=failed``），运行时照旧回落 LLM——**编译失败绝不阻断主链路**。

识别不了的条件一律编译成 ``llm_judge`` 也同样是安全结果：求值恒返回
``None``，等于「这条边仍交给模型判断」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from sqlmodel import select

from app import paths
from app.db.models import SkillEdgeCondition, utc_now
from app.skills.edge_condition_spec import (
    COMPILER_VERSION,
    EdgeConditionSpec,
    KIND_LABELS,
    SlotComparison,
    condition_fingerprint,
    spec_from_condition_text,
    spec_from_payload,
)

logger = logging.getLogger(__name__)

PROMPT_PATH = (
    paths.resource_dir() / "app" / "llm" / "prompts" / "edge_condition_compiler_prompt.md"
)

# 与契约 kind 集合保持一致（LLM 只能从这几个里选）
ALLOWED_KINDS = (
    "always",
    "slots_all",
    "slots_any",
    "slots_missing",
    "slot_compare",
    "user_confirmed",
    "user_rejected",
    "result_ok",
    "result_failed",
    "llm_judge",
)
ALLOWED_OPS = ("eq", "ne", "contains", "in", "gte", "lte")

STATUS_COMPILED = "compiled"
STATUS_LLM_JUDGE = "llm_judge"
STATUS_FAILED = "failed"

# 单次 LLM 编译的最大条件边数（超出分批；防止畸形大图把一次调用撑爆）
MAX_EDGES_PER_CALL = 60


@dataclass
class CompiledEdge:
    """一条边的编译结果（内存对象，落库由 ``upsert_edge_conditions`` 负责）。"""

    source_node_id: str
    next_node_id: str
    condition_text: str
    fingerprint: str
    status: str
    spec: EdgeConditionSpec | None
    source: str = "llm"
    confidence: float = 1.0
    error: str | None = None

    @property
    def kind(self) -> str:
        return self.spec.kind if self.spec is not None else ""

    def to_row_payload(self) -> dict[str, Any]:
        return {
            "source_node_id": self.source_node_id,
            "next_node_id": self.next_node_id,
            "condition_text": self.condition_text,
            "condition_fingerprint": self.fingerprint,
            "status": self.status,
            "kind": self.kind,
            "spec_json": self.spec.model_dump(mode="json") if self.spec is not None else {},
            "source": self.source,
            "confidence": self.confidence,
            "error": self.error,
        }

    def review_line(self) -> str:
        condition = self.condition_text or "（无条件）"
        compiled = self.spec.readable() if self.spec is not None else "编译失败（保持自由文本）"
        return f"{self.source_node_id} → {self.next_node_id}｜{condition}｜{compiled}"


@dataclass
class CompileOutcome:
    edges: list[CompiledEdge] = field(default_factory=list)
    llm_calls: int = 0
    llm_error: str | None = None

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.edges:
            key = item.kind or item.status
            counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def deterministic_count(self) -> int:
        return sum(1 for item in self.edges if item.status == STATUS_COMPILED)


# ---------------------------------------------------------------------------
# 图遍历
# ---------------------------------------------------------------------------


def iter_edges(content: Mapping[str, Any]) -> list[dict[str, Any]]:
    edges = content.get("edges") if isinstance(content, Mapping) else None
    if not isinstance(edges, list):
        return []
    return [item for item in edges if isinstance(item, dict)]


def node_index(content: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = content.get("nodes") if isinstance(content, Mapping) else None
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(nodes, list):
        return result
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("node_id") or "").strip()
        if node_id:
            result[node_id] = node
    return result


def node_required_fields(node: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(node, Mapping):
        return []
    values = node.get("expected_user_info")
    if not isinstance(values, list):
        return []
    fields: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in fields:
            fields.append(text)
    return fields


def _graph_tokens(content: Mapping[str, Any]) -> set[str]:
    """整张图里出现过的槽位字段集合（供 LLM 判断条件引用的字段是否真实存在）。"""

    tokens: set[str] = set()
    for node in node_index(content).values():
        for item in node.get("expected_user_info") or []:
            text = str(item or "").strip()
            if text:
                tokens.add(text)
    for item in content.get("required_info") or []:
        text = str(item or "").strip()
        if text:
            tokens.add(text)
    return tokens


# ---------------------------------------------------------------------------
# 编译主流程
# ---------------------------------------------------------------------------


def compile_skill_conditions(
    content: Mapping[str, Any],
    *,
    model_config: Any = None,
    trace: Any = None,
    allow_llm: bool = True,
) -> CompileOutcome:
    """编译一张 SOP 图的全部条件边。

    ``content`` 是 ``SkillCard.model_dump(mode="json")`` 形态的 dict。
    只处理**非无条件**的边（无条件边即 ``always``，由 ``spec_from_condition_text``
    直译，不需要 LLM）；``allow_llm=False`` 时只跑预设直译（供脚本 ``--no-llm``
    与纯离线场景使用）。
    """

    nodes = node_index(content)
    outcome = CompileOutcome()
    pending: list[tuple[dict[str, Any], dict[str, Any] | None, list[str]]] = []

    for edge in iter_edges(content):
        source_id = str(edge.get("source_node_id") or "").strip()
        next_id = str(edge.get("next_node_id") or "").strip()
        if not source_id or not next_id:
            continue
        condition = edge.get("condition")
        source_node = nodes.get(source_id)
        required_fields = node_required_fields(source_node)
        fingerprint = condition_fingerprint(source_id, next_id, condition, required_fields)

        # 1) 已有结构化契约（人工确认 / 前次编译保留在边上的情况）优先
        embedded = spec_from_payload(edge.get("condition_spec"))
        if embedded is not None:
            outcome.edges.append(
                _compiled(
                    source_id,
                    next_id,
                    condition,
                    fingerprint,
                    embedded,
                    source=embedded.source,
                    confidence=embedded.confidence,
                )
            )
            continue

        # 2) 预设 / 无条件直译（零 LLM）
        preset = spec_from_condition_text(condition)
        if preset is not None:
            outcome.edges.append(
                _compiled(
                    source_id,
                    next_id,
                    condition,
                    fingerprint,
                    preset,
                    source=preset.source,
                    confidence=preset.confidence,
                )
            )
            continue

        pending.append((edge, source_node, required_fields))

    if pending and allow_llm and model_config is not None:
        outcome.edges.extend(
            _compile_with_llm(content, pending, model_config, outcome, trace=trace)
        )
    else:
        for edge, _node, required_fields in pending:
            source_id = str(edge.get("source_node_id") or "").strip()
            next_id = str(edge.get("next_node_id") or "").strip()
            condition = edge.get("condition")
            fingerprint = condition_fingerprint(source_id, next_id, condition, required_fields)
            outcome.edges.append(
                CompiledEdge(
                    source_node_id=source_id,
                    next_node_id=next_id,
                    condition_text=_text(condition),
                    fingerprint=fingerprint,
                    status=STATUS_LLM_JUDGE,
                    spec=EdgeConditionSpec(
                        kind="llm_judge",
                        source="preset",
                        confidence=0.0,
                        rationale="未启用 LLM 编译，保持交由模型判断。",
                    ),
                    source="preset",
                    confidence=0.0,
                )
            )
    return outcome


def _compiled(
    source_id: str,
    next_id: str,
    condition: Any,
    fingerprint: str,
    spec: EdgeConditionSpec,
    *,
    source: str,
    confidence: float,
) -> CompiledEdge:
    status = STATUS_COMPILED if spec.is_deterministic() else STATUS_LLM_JUDGE
    return CompiledEdge(
        source_node_id=source_id,
        next_node_id=next_id,
        condition_text=_text(condition),
        fingerprint=fingerprint,
        status=status,
        spec=spec,
        source=source,
        confidence=confidence,
    )


def _compile_with_llm(
    content: Mapping[str, Any],
    pending: Sequence[tuple[dict[str, Any], dict[str, Any] | None, list[str]]],
    model_config: Any,
    outcome: CompileOutcome,
    *,
    trace: Any = None,
) -> list[CompiledEdge]:
    results: list[CompiledEdge] = []
    for batch in _chunks(list(pending), MAX_EDGES_PER_CALL):
        payload, context = _build_payload(content, batch)
        raw: Any = None
        error: str | None = None
        try:
            from app.llm import LLMClient
            from app.observability.spans import llm_operation

            client = LLMClient(model_config)
            with llm_operation("sop.edge_condition_compile"):
                raw = client.generate_json(
                    PROMPT_PATH.read_text(encoding="utf-8").strip(),
                    payload,
                )
            outcome.llm_calls += 1
        except Exception as exc:  # noqa: BLE001 - 编译失败保持自由文本，绝不阻断
            error = str(exc)
            outcome.llm_error = error
            outcome.llm_calls += 1
            logger.warning("SOP 边条件 LLM 编译失败：%s", exc, exc_info=True)
        specs = _match_specs(raw, context) if raw is not None else {}
        for edge, _node, required_fields in batch:
            source_id = str(edge.get("source_node_id") or "").strip()
            next_id = str(edge.get("next_node_id") or "").strip()
            condition = edge.get("condition")
            fingerprint = condition_fingerprint(source_id, next_id, condition, required_fields)
            spec = specs.get((source_id, next_id))
            if spec is None:
                results.append(
                    CompiledEdge(
                        source_node_id=source_id,
                        next_node_id=next_id,
                        condition_text=_text(condition),
                        fingerprint=fingerprint,
                        status=STATUS_FAILED,
                        spec=None,
                        source="llm",
                        confidence=0.0,
                        error=error or "模型未返回该边的结构化条件",
                    )
                )
                continue
            results.append(
                _compiled(
                    source_id,
                    next_id,
                    condition,
                    fingerprint,
                    spec,
                    source="llm",
                    confidence=spec.confidence,
                )
            )
    _emit(trace, outcome, len(results))
    return results


def _build_payload(
    content: Mapping[str, Any],
    batch: Sequence[tuple[dict[str, Any], dict[str, Any] | None, list[str]]],
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    nodes = node_index(content)
    graph_fields = _graph_tokens(content)
    context: dict[tuple[str, str], dict[str, Any]] = {}
    items: list[dict[str, Any]] = []
    for edge, source_node, required_fields in batch:
        source_id = str(edge.get("source_node_id") or "").strip()
        next_id = str(edge.get("next_node_id") or "").strip()
        target_node = nodes.get(next_id) or {}
        context[(source_id, next_id)] = {
            # 源节点声明的必填字段（fields 为空时的退化作用域）
            "required_fields": required_fields,
            # 整图声明过的槽位字段并集（条件允许引用的字段白名单）
            "slot_fields": graph_fields,
            "target_name": str(target_node.get("name") or ""),
        }
        items.append(
            {
                "source_node_id": source_id,
                "next_node_id": next_id,
                "condition": _text(edge.get("condition")),
                "source_node": _node_brief(source_node, required_fields),
                "target_node": _node_brief(target_node, node_required_fields(target_node)),
                "priority": edge.get("priority"),
                "label": edge.get("label"),
            }
        )
    payload = {
        "skill_name": str(content.get("name") or ""),
        "skill_goal": content.get("goal") or [],
        "required_info": content.get("required_info") or [],
        "known_slot_fields": sorted(_graph_tokens(content)),
        "conditions": items,
        "allowed_kinds": list(ALLOWED_KINDS),
        "allowed_ops": list(ALLOWED_OPS),
    }
    return payload, context


def _node_brief(node: Mapping[str, Any] | None, required_fields: Sequence[str]) -> dict[str, Any]:
    if not isinstance(node, Mapping):
        return {}
    return {
        "node_id": str(node.get("node_id") or ""),
        "type": str(node.get("type") or ""),
        "name": str(node.get("name") or ""),
        "instruction": _text(node.get("instruction"))[:600],
        "expected_user_info": list(required_fields),
        "allowed_actions": [
            str(item) for item in (node.get("allowed_actions") or []) if str(item).strip()
        ],
    }


def _match_specs(
    raw: Any,
    context: Mapping[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], EdgeConditionSpec]:
    """把模型输出对齐回 (source, next) → spec；任何异常项直接丢弃（保守）。"""

    items: list[Any] = []
    if isinstance(raw, dict):
        candidate = raw.get("conditions")
        if isinstance(candidate, list):
            items = candidate
    elif isinstance(raw, list):
        items = raw
    resolved: dict[tuple[str, str], EdgeConditionSpec] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_node_id") or "").strip()
        next_id = str(item.get("next_node_id") or "").strip()
        key = (source_id, next_id)
        if key not in context:
            continue
        spec = _sanitize_spec(item, context[key])
        if spec is not None:
            resolved[key] = spec
    return resolved


def _sanitize_spec(
    item: Mapping[str, Any],
    ctx: Mapping[str, Any],
) -> EdgeConditionSpec | None:
    kind = str(item.get("kind") or "").strip()
    if kind not in ALLOWED_KINDS:
        return None
    # 图上声明过的槽位字段并集：模型臆造的字段名会让判定永远落空（或误判为
    # 「字段缺失」），一律丢弃。
    allowed_fields = {str(field) for field in ctx.get("slot_fields") or []}
    # 该边源节点自己声明的必填字段（fields 为空时的退化作用域）
    source_required = {str(field) for field in ctx.get("required_fields") or []}
    fields: list[str] = []
    for value in item.get("fields") or []:
        text = str(value or "").strip()
        if not text:
            continue
        if allowed_fields and text not in allowed_fields:
            continue
        if text not in fields:
            fields.append(text)
    comparisons: list[SlotComparison] = []
    for value in item.get("comparisons") or []:
        if not isinstance(value, dict):
            continue
        field = str(value.get("field") or "").strip()
        op = str(value.get("op") or "contains").strip()
        if not field or op not in ALLOWED_OPS:
            continue
        if allowed_fields and field not in allowed_fields:
            continue
        comparisons.append(SlotComparison(field=field, op=op, value=value.get("value")))
    if kind in {"slots_all", "slots_any", "slots_missing"} and not fields:
        # 字段为空 → 退化为「该边源节点声明的必填字段」；源节点无必填字段则不可判定
        if not source_required:
            return EdgeConditionSpec(
                kind="llm_judge",
                source="llm",
                confidence=0.0,
                rationale="条件引用的字段无法解析，保持交由模型判断。",
            )
    if kind == "slot_compare" and not comparisons:
        return None
    try:
        confidence = float(item.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return EdgeConditionSpec(
        kind=kind,  # type: ignore[arg-type]
        fields=fields,
        comparisons=comparisons,
        confidence=min(max(confidence, 0.0), 1.0),
        rationale=_text(item.get("rationale"))[:400],
        source="llm",
        compiler_version=COMPILER_VERSION,
    )


def _emit(trace: Any, outcome: CompileOutcome, count: int) -> None:
    if callable(trace):
        try:
            trace(
                "sop_edge_condition_compiled",
                {
                    "compiled": count,
                    "llm_calls": outcome.llm_calls,
                    "stats": outcome.stats(),
                    "error": outcome.llm_error,
                },
            )
        except Exception:  # noqa: BLE001 - 观测失败不影响主链路
            pass


def _chunks(items: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


def upsert_edge_conditions(
    db: Any,
    tenant_id: str,
    skill_id: str,
    version: str,
    edges: Sequence[CompiledEdge],
    *,
    prune_stale: bool = True,
) -> int:
    """写入编译结果；同指纹覆盖，可选清理当前图上已不存在的旧行。

    ``prune_stale`` 只在**整张图都编译过**时才有意义（脚本 / 保存时调用），
    因此调用方需要保证 ``edges`` 覆盖当前图的全部条件边。
    """

    now = utc_now()
    existing_rows = list(
        db.exec(
            select(SkillEdgeCondition).where(
                SkillEdgeCondition.tenant_id == tenant_id,
                SkillEdgeCondition.skill_id == skill_id,
            )
        ).all()
    )
    by_fingerprint = {row.condition_fingerprint: row for row in existing_rows}
    keep: set[str] = set()
    written = 0
    for edge in edges:
        keep.add(edge.fingerprint)
        payload = edge.to_row_payload()
        row = by_fingerprint.get(edge.fingerprint)
        if row is None:
            row = SkillEdgeCondition(
                tenant_id=tenant_id,
                skill_id=skill_id,
                version=version,
                **payload,
            )
            db.add(row)
        else:
            for key, value in payload.items():
                setattr(row, key, value)
            row.version = version
            row.updated_at = now
            db.add(row)
        written += 1
    if prune_stale and keep:
        for row in existing_rows:
            if row.condition_fingerprint not in keep:
                db.delete(row)
    return written


def load_edge_condition_specs(db: Any, tenant_id: str, skill_id: str) -> dict[str, EdgeConditionSpec]:
    """按指纹加载一张 SOP 的全部结构化边条件（运行时热路径入口）。

    返回 ``{fingerprint: spec}``；运行时用 ``condition_fingerprint(...)`` 现算
    指纹查表——条件原文或节点必填字段一变，指纹就对不上，旧编译结果自然失效。
    """

    rows = list(
        db.exec(
            select(SkillEdgeCondition).where(
                SkillEdgeCondition.tenant_id == tenant_id,
                SkillEdgeCondition.skill_id == skill_id,
            )
        ).all()
    )
    result: dict[str, EdgeConditionSpec] = {}
    for row in rows:
        spec = spec_from_payload(row.spec_json)
        if spec is None:
            continue
        result[row.condition_fingerprint] = spec
    return result


def list_edge_condition_rows(
    db: Any,
    tenant_id: str,
    skill_id: str,
) -> list[SkillEdgeCondition]:
    """人工复核用：按节点顺序列出编译结果。"""

    return list(
        db.exec(
            select(SkillEdgeCondition).where(
                SkillEdgeCondition.tenant_id == tenant_id,
                SkillEdgeCondition.skill_id == skill_id,
            )
        ).all()
    )


def condition_fingerprint_for_edge(
    content: Mapping[str, Any],
    edge: Mapping[str, Any],
) -> str:
    """便捷函数：给定图与边，算指纹（校验编译结果是否仍适用）。"""

    nodes = node_index(content)
    source_id = str(edge.get("source_node_id") or "").strip()
    next_id = str(edge.get("next_node_id") or "").strip()
    required_fields = node_required_fields(nodes.get(source_id))
    return condition_fingerprint(source_id, next_id, edge.get("condition"), required_fields)


def summarize_kinds(edges: Sequence[CompiledEdge]) -> str:
    """可读的编译结果摘要（脚本输出用）。"""

    if not edges:
        return "（无条件边）"
    counts: dict[str, int] = {}
    for item in edges:
        key = item.kind or item.status
        counts[key] = counts.get(key, 0) + 1
    parts = [f"{KIND_LABELS.get(key, key)}×{value}" for key, value in sorted(counts.items())]
    return "，".join(parts)


__all__ = [
    "ALLOWED_KINDS",
    "CompileOutcome",
    "CompiledEdge",
    "STATUS_COMPILED",
    "STATUS_FAILED",
    "STATUS_LLM_JUDGE",
    "compile_skill_conditions",
    "condition_fingerprint_for_edge",
    "iter_edges",
    "load_edge_condition_specs",
    "list_edge_condition_rows",
    "node_index",
    "node_required_fields",
    "summarize_kinds",
    "upsert_edge_conditions",
]
