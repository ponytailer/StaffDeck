"""SOP 边条件离线编译器的测试。

覆盖三条关键性质：
1. **零 LLM 就能编译的比例**：前端预设与无条件边必须走确定性直译；
2. **LLM 结果的清洗**：模型臆造的字段名 / 非法 kind 一律丢弃，不能污染运行时判定；
3. **指纹表语义**：按 (tenant, skill_id, fingerprint) 复用，条件一改旧行自然失效。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from app.db.models import SkillEdgeCondition
from app.skills.edge_condition_compiler import (
    CompiledEdge,
    compile_skill_conditions,
    condition_fingerprint_for_edge,
    load_edge_condition_specs,
    summarize_kinds,
    upsert_edge_conditions,
)
from app.skills.edge_condition_spec import (
    EdgeConditionSpec,
    SlotComparison,
    condition_fingerprint,
)
from app.skills.edge_condition_jobs import edge_condition_review


TENANT = "tenant_demo"


def _graph() -> dict[str, Any]:
    return {
        "name": "权限开通",
        "goal": ["开通权限"],
        "required_info": [],
        "nodes": [
            {
                "node_id": "n1",
                "type": "collect_info",
                "name": "收集信息",
                "instruction": "收集工号与权限级别",
                "expected_user_info": ["工号", "权限级别"],
            },
            {
                "node_id": "n2",
                "type": "decision",
                "name": "判断",
                "instruction": "根据权限级别分流",
                "expected_user_info": [],
            },
            {"node_id": "n3", "type": "handoff", "name": "转人工"},
        ],
        "edges": [
            {"source_node_id": "n1", "next_node_id": "n2", "condition": "所有必填信息都收集完成后进入"},
            {"source_node_id": "n1", "next_node_id": "n1", "condition": "还有必填信息没有收集到时进入"},
            {"source_node_id": "n2", "next_node_id": "n3", "condition": "如果权限级别是管理员就转人工"},
            {"source_node_id": "n2", "next_node_id": "n2", "condition": "上一步工具调用成功后进入"},
        ],
    }


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


class _FakeClient:
    """替代 LLMClient：只接收 payload、返回预置 JSON。"""

    calls: list[dict[str, Any]] = []
    response: dict[str, Any] = {}

    def __init__(self, model_config: Any) -> None:
        self.model_config = model_config

    def generate_json(self, system_prompt: str, payload: dict[str, Any]) -> Any:
        _FakeClient.calls.append(payload)
        return _FakeClient.response


def _model_config() -> Any:
    # 编译器只把 model_config 传给 LLMClient 构造函数，测试里给个占位即可
    return object()


# ---------------------------------------------------------------------------
# 确定性直译
# ---------------------------------------------------------------------------


def test_preset_edges_compile_without_llm() -> None:
    outcome = compile_skill_conditions(_graph(), model_config=None)
    assert outcome.llm_calls == 0
    by_target = {(item.source_node_id, item.next_node_id): item for item in outcome.edges}
    assert by_target[("n1", "n2")].kind == "slots_all"
    assert by_target[("n1", "n1")].kind == "slots_missing"
    assert by_target[("n2", "n2")].kind == "result_ok"
    # 自由文本在禁用 LLM 时保持「交模型判断」，不会被瞎猜
    assert by_target[("n2", "n3")].kind == "llm_judge"
    assert by_target[("n2", "n3")].status == "llm_judge"
    assert outcome.deterministic_count == 3
    assert "指定字段全部已收集" in summarize_kinds(outcome.edges)


def test_no_llm_pass_never_produces_a_deterministic_guess() -> None:
    outcome = compile_skill_conditions(_graph(), model_config=None)
    for item in outcome.edges:
        if item.spec is not None and item.spec.kind == "llm_judge":
            assert item.spec.confidence == 0.0
            assert item.status == "llm_judge"


def test_unconditional_edges_compile_to_always() -> None:
    graph = {
        "nodes": [{"node_id": "a"}, {"node_id": "b", "expected_user_info": []}],
        "edges": [{"source_node_id": "a", "next_node_id": "b", "condition": ""}],
    }
    outcome = compile_skill_conditions(graph, model_config=None)
    assert [item.kind for item in outcome.edges] == ["always"]
    assert outcome.llm_calls == 0


def test_edges_with_missing_endpoints_are_skipped() -> None:
    graph = {
        "nodes": [{"node_id": "a"}],
        "edges": [
            {"source_node_id": "a", "condition": "用户确认后"},
            {"source_node_id": "a", "next_node_id": "b", "condition": "用户确认后"},
        ],
    }
    outcome = compile_skill_conditions(graph, model_config=None)
    assert len(outcome.edges) == 1


# ---------------------------------------------------------------------------
# LLM 编译与结果清洗
# ---------------------------------------------------------------------------


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> None:
    _FakeClient.calls = []
    _FakeClient.response = response
    monkeypatch.setattr("app.llm.LLMClient", _FakeClient)


def test_llm_compiles_only_the_free_text_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        {
            "conditions": [
                {
                    "source_node_id": "n2",
                    "next_node_id": "n3",
                    "kind": "slot_compare",
                    "fields": [],
                    "comparisons": [{"field": "权限级别", "op": "contains", "value": "管理员"}],
                    "confidence": 0.9,
                    "rationale": "权限级别包含管理员",
                }
            ]
        },
    )
    outcome = compile_skill_conditions(_graph(), model_config=_model_config())
    assert outcome.llm_calls == 1
    # 一次调用只处理自由文本边，预设边不进 payload（省 token、避免模型改判）
    payload = _FakeClient.calls[0]
    assert [item["condition"] for item in payload["conditions"]] == ["如果权限级别是管理员就转人工"]
    by_target = {(item.source_node_id, item.next_node_id): item for item in outcome.edges}
    spec = by_target[("n2", "n3")].spec
    assert spec is not None
    assert spec.kind == "slot_compare"
    assert spec.comparisons[0].field == "权限级别"
    assert by_target[("n2", "n3")].status == "compiled"


def test_llm_invented_field_names_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型臆造字段 → 丢弃；字段清空后可退化，退化不了就降级 llm_judge。"""

    _patch_llm(
        monkeypatch,
        {
            "conditions": [
                {
                    "source_node_id": "n2",
                    "next_node_id": "n3",
                    "kind": "slots_all",
                    "fields": ["不存在的字段"],
                    "confidence": 1.0,
                }
            ]
        },
    )
    outcome = compile_skill_conditions(_graph(), model_config=_model_config())
    item = next(x for x in outcome.edges if x.next_node_id == "n3")
    assert item.spec is not None
    assert item.spec.fields == []
    # n2 没有声明必填字段 → 无从退化，必须降级为交模型判断
    assert item.spec.kind == "llm_judge"


def test_llm_invalid_kind_is_discarded(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        {"conditions": [{"source_node_id": "n2", "next_node_id": "n3", "kind": "随便编的"}]},
    )
    outcome = compile_skill_conditions(_graph(), model_config=_model_config())
    item = next(x for x in outcome.edges if x.next_node_id == "n3")
    assert item.status == "failed"
    assert item.spec is None


def test_llm_failure_keeps_free_text_and_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_FakeClient):
        def generate_json(self, system_prompt: str, payload: dict[str, Any]) -> Any:
            raise RuntimeError("gateway down")

    monkeypatch.setattr("app.llm.LLMClient", _Boom)
    outcome = compile_skill_conditions(_graph(), model_config=_model_config())
    assert outcome.llm_error is not None
    item = next(x for x in outcome.edges if x.next_node_id == "n3")
    assert item.status == "failed"
    assert item.spec is None


def test_llm_unknown_edge_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型返回了不在本次批次里的边 → 丢弃（不能凭空生成编译结果）。"""

    _patch_llm(
        monkeypatch,
        {
            "conditions": [
                {"source_node_id": "n9", "next_node_id": "n9", "kind": "always"},
                {"source_node_id": "n2", "next_node_id": "n3", "kind": "always"},
            ]
        },
    )
    outcome = compile_skill_conditions(_graph(), model_config=_model_config())
    item = next(x for x in outcome.edges if x.next_node_id == "n3")
    assert item.kind == "always"


# ---------------------------------------------------------------------------
# 指纹表写入 / 读取
# ---------------------------------------------------------------------------


def _compiled_for(graph: dict[str, Any]) -> list[CompiledEdge]:
    return compile_skill_conditions(graph, model_config=None).edges


def test_upsert_and_load_roundtrip() -> None:
    session = _test_session()
    graph = _graph()
    edges = _compiled_for(graph)
    written = upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", edges)
    session.commit()
    assert written == len(edges)

    specs = load_edge_condition_specs(session, TENANT, "sop-1")
    assert len(specs) == len(edges)
    for item in edges:
        spec = specs[item.fingerprint]
        assert spec.kind == item.kind

    # 重复写入幂等：不新增行，只覆盖
    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.1", edges)
    session.commit()
    assert len(session.exec(select(SkillEdgeCondition)).all()) == len(edges)


def test_changed_condition_yields_a_new_fingerprint_and_row() -> None:
    session = _test_session()
    graph = _graph()
    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", _compiled_for(graph))
    session.commit()

    graph["edges"][2]["condition"] = "如果权限级别是普通用户就转人工"
    upsert_edge_conditions(
        session,
        TENANT,
        "sop-1",
        "1.1.0",
        _compiled_for(graph),
        prune_stale=False,
    )
    session.commit()

    # 旧指纹的行还在（prune 由调用方按「全图在用集合」决定），但运行时按新
    # 指纹查表，所以拿不到旧结果——条件一改，旧编译结果自然失效
    old_fingerprint = condition_fingerprint("n2", "n3", "如果权限级别是管理员就转人工", [])
    new_fingerprint = condition_fingerprint("n2", "n3", "如果权限级别是普通用户就转人工", [])
    specs = load_edge_condition_specs(session, TENANT, "sop-1")
    assert old_fingerprint in specs
    assert new_fingerprint in specs
    assert old_fingerprint != new_fingerprint


def test_prune_stale_removes_orphan_rows() -> None:
    session = _test_session()
    graph = _graph()
    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", _compiled_for(graph))
    session.commit()

    trimmed = {
        "nodes": graph["nodes"],
        "edges": [
            {
                "source_node_id": "n1",
                "next_node_id": "n2",
                "condition": "所有必填信息都收集完成后进入",
            }
        ],
    }
    upsert_edge_conditions(session, TENANT, "sop-1", "2.0.0", _compiled_for(trimmed))
    session.commit()

    rows = session.exec(select(SkillEdgeCondition)).all()
    assert len(rows) == 1
    assert rows[0].next_node_id == "n2"


def test_condition_fingerprint_for_edge_matches_compiler_output() -> None:
    """运行时（按图算指纹）与编译期（按图算指纹）必须是同一个函数、同一结果。"""

    graph = _graph()
    compiled = {
        (item.source_node_id, item.next_node_id): item.fingerprint
        for item in _compiled_for(graph)
    }
    for edge in graph["edges"]:
        key = (edge["source_node_id"], edge["next_node_id"])
        assert condition_fingerprint_for_edge(graph, edge) == compiled[key]


# ---------------------------------------------------------------------------
# 人工复核视图
# ---------------------------------------------------------------------------


def _seed_skill(session: Session, graph: dict[str, Any]) -> None:
    from app.db.models import Skill

    session.add(
        Skill(
            tenant_id=TENANT,
            skill_id="sop-1",
            version="1.0.0",
            name="权限开通",
            content_json=graph,
            status="published",
        )
    )
    session.commit()


def test_review_lists_every_conditional_edge_with_status() -> None:
    session = _test_session()
    graph = _graph()
    _seed_skill(session, graph)

    review = edge_condition_review(session, tenant_id=TENANT, skill_id="sop-1")
    # 4 条条件边全部列出，未编译时状态是 pending
    assert review["total"] == 4
    assert review["pending"] == 4
    assert {item["status"] for item in review["conditions"]} == {"pending"}

    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", _compiled_for(graph))
    session.commit()

    review = edge_condition_review(session, tenant_id=TENANT, skill_id="sop-1")
    assert review["pending"] == 0
    assert review["stats"]["compiled"] == 3
    assert review["stats"]["llm_judge"] == 1
    compiled_item = next(item for item in review["conditions"] if item["status"] == "compiled")
    assert compiled_item["readable"]
    assert compiled_item["kind_label"]


def test_review_skips_unconditional_edges() -> None:
    session = _test_session()
    graph = {
        "nodes": [{"node_id": "a"}, {"node_id": "b"}],
        "edges": [{"source_node_id": "a", "next_node_id": "b", "condition": ""}],
    }
    _seed_skill(session, graph)
    review = edge_condition_review(session, tenant_id=TENANT, skill_id="sop-1")
    assert review["total"] == 0
    assert review["pending"] == 0


def test_review_marks_failed_edges_as_pending() -> None:
    """编译失败的行不能算「已编译」——否则重编永远不会被触发。"""

    from app.skills.edge_condition_jobs import has_uncompiled_edge

    session = _test_session()
    graph = _graph()
    _seed_skill(session, graph)
    edges = _compiled_for(graph)
    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", edges, prune_stale=False)
    # 把其中一条标成 failed
    row = session.exec(select(SkillEdgeCondition)).first()
    assert row is not None
    row.status = "failed"
    session.add(row)
    session.commit()

    assert has_uncompiled_edge(session, TENANT, "sop-1", graph) is True


def test_has_uncompiled_edge_false_when_everything_covered() -> None:
    from app.skills.edge_condition_jobs import has_uncompiled_edge

    session = _test_session()
    graph = _graph()
    _seed_skill(session, graph)
    assert has_uncompiled_edge(session, TENANT, "sop-1", graph) is True
    upsert_edge_conditions(session, TENANT, "sop-1", "1.0.0", _compiled_for(graph))
    session.commit()
    assert has_uncompiled_edge(session, TENANT, "sop-1", graph) is False


def test_embedding_spec_on_the_edge_wins() -> None:
    """边自带 condition_spec（人工确认过的结果）优先于预设直译。"""

    graph = {
        "nodes": [{"node_id": "a", "expected_user_info": ["x"]}, {"node_id": "b"}],
        "edges": [
            {
                "source_node_id": "a",
                "next_node_id": "b",
                "condition": "上一步工具调用成功后进入",
                "condition_spec": {
                    "kind": "slot_compare",
                    "comparisons": [{"field": "x", "op": "eq", "value": "y"}],
                    "source": "manual",
                },
            }
        ],
    }
    outcome = compile_skill_conditions(graph, model_config=None)
    assert len(outcome.edges) == 1
    spec = outcome.edges[0].spec
    assert spec is not None
    assert spec.kind == "slot_compare"
    assert spec.source == "manual"


def test_manual_spec_is_never_overwritten_by_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, {"conditions": []})
    graph = {
        "nodes": [{"node_id": "a"}, {"node_id": "b"}],
        "edges": [
            {
                "source_node_id": "a",
                "next_node_id": "b",
                "condition": "只看条款第三条",
                "condition_spec": {"kind": "user_confirmed"},
            }
        ],
    }
    outcome = compile_skill_conditions(graph, model_config=_model_config())
    assert outcome.llm_calls == 0
    assert outcome.edges[0].kind == "user_confirmed"


def test_compiled_spec_is_deterministic_for_backend_reuse() -> None:
    """编译产物必须能被运行时直接求值（契约与求值器签名对齐）。"""

    from app.skills.edge_condition_spec import EdgeEvalContext, evaluate_edge_condition

    spec = EdgeConditionSpec(
        kind="slot_compare",
        comparisons=[SlotComparison(field="权限级别", op="contains", value="管理员")],
    )
    assert evaluate_edge_condition(
        spec,
        EdgeEvalContext(slots={"权限级别": "生产环境管理员"}, node_required_fields=("权限级别",)),
    ) is True
