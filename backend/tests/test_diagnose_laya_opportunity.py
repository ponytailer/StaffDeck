"""``scripts/diagnose_laya_opportunity.py`` 的聚合逻辑单测。

脚本是只读诊断，跑的是生产库；这里用内存 sqlite 造最小事件集，钉死：
- 知识检索路由分类（走 LLM vs 缓存/短路/词法）
- LLM operation 的调用次数与耗时分布
- SOP 边条件 kind 分布、TurnPlanner decision 分布
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


BACKEND_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = BACKEND_DIR / "scripts" / "diagnose_laya_opportunity.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("diagnose_laya_opportunity", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["diagnose_laya_opportunity"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE agent_events ("
                "id INTEGER PRIMARY KEY, tenant_id TEXT, session_id TEXT, "
                "event_type TEXT, payload_json TEXT, created_at TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE skill_edge_conditions ("
                "id INTEGER PRIMARY KEY, tenant_id TEXT, skill_id TEXT, kind TEXT, status TEXT)"
            )
        )
        events = [
            ("llm_call_finished", {"operation": "knowledge.document_route", "duration_ms": 4400}),
            ("llm_call_finished", {"operation": "knowledge.document_route", "duration_ms": 2200}),
            ("llm_call_finished", {"operation": "knowledge.bucket_route", "duration_ms": 3000}),
            ("llm_call_finished", {"operation": "turn_planner.plan", "duration_ms": 15000}),
            (
                "harness_tool_completed",
                {
                    "tool_name": "knowledge_search",
                    "success": True,
                    "result": {"route_phases": ["document_route", "route_cache_stored"]},
                },
            ),
            (
                "harness_tool_completed",
                {
                    "tool_name": "knowledge_search",
                    "success": True,
                    "result": {"route_phases": ["document_route", "document_route_lexical_fast_path"]},
                },
            ),
            (
                "harness_tool_completed",
                {
                    "tool_name": "knowledge_search",
                    "success": True,
                    "result": {"route_phases": ["document_route_small_candidate_shortcut"]},
                },
            ),
            (
                "harness_tool_completed",
                {"tool_name": "knowledge_search", "success": False, "result": {}},
            ),
            (
                "harness_tool_completed",
                {"tool_name": "http_request", "success": True, "result": {}},
            ),
            ("sop_prefill_planned", {"scene": "condition_spec_direct_eval"}),
            ("sop_prefill_planned", {"scene": "laya_edge_decision"}),
            (
                "turn_plan_created",
                {"decision": "answer_only", "task_frames": [{"kind": "conversation"}]},
            ),
            (
                "turn_plan_created",
                {"decision": "start_new_task", "task_frames": [{"kind": "sop"}]},
            ),
        ]
        for event_type, payload in events:
            connection.execute(
                text(
                    "INSERT INTO agent_events (event_type, payload_json) VALUES (:e, :p)"
                ),
                {"e": event_type, "p": json.dumps(payload, ensure_ascii=False)},
            )
        for kind, status, count in (("llm_judge", "compiled", 3), ("slots_all", "compiled", 7)):
            for _ in range(count):
                connection.execute(
                    text(
                        "INSERT INTO skill_edge_conditions (kind, status) VALUES (:k, :s)"
                    ),
                    {"k": kind, "s": status},
                )
    with engine.connect() as connection:
        yield connection


def test_percentiles_use_nearest_rank() -> None:
    module = _load_script()
    stats = module.percentiles([10.0, 20.0, 30.0, 40.0])
    assert stats[50] == 20.0
    assert stats[90] == 40.0
    assert module.percentiles([]) == {50: 0.0, 90: 0.0, 99: 0.0}


def test_categorize_route_priority() -> None:
    module = _load_script()
    assert module.categorize_route(["document_route", "route_cache_stored"]) == "llm_route"
    assert module.categorize_route(["document_route", "document_route_lexical_fast_path"]) == "lexical_fast_path"
    assert module.categorize_route(["document_route_small_candidate_shortcut"]) == "small_candidate_shortcut"
    assert module.categorize_route(["document_route_cache_hit", "document_route"]) == "route_cache_hit"
    assert module.categorize_route(["document_route_lexical_fallback"]) == "llm_route_failed_fallback"
    assert module.categorize_route(["okf_only"]) == "okf_only"


def test_collect_stats_aggregates_knowledge_and_llm(conn) -> None:
    module = _load_script()
    report = module.collect_stats(conn, tenant_id=None, since=None, limit=1000)

    knowledge = report["knowledge"]
    assert knowledge["searches"] == 4
    assert knowledge["failed"] == 1
    assert knowledge["route_llm_calls"] == 3
    assert knowledge["route_llm_calls_per_search"] == 0.75
    assert knowledge["route_categories"] == {
        "llm_route": 1,
        "lexical_fast_path": 1,
        "small_candidate_shortcut": 1,
        "no_phases": 1,
    }

    document = report["llm_operations"]["knowledge.document_route"]
    assert document["count"] == 2
    assert document["p90"] == 4400.0
    assert report["llm_operations"]["knowledge.bucket_route"]["count"] == 1


def test_collect_stats_aggregates_sop_and_planner(conn) -> None:
    module = _load_script()
    report = module.collect_stats(conn, tenant_id=None, since=None, limit=1000)

    assert report["sop_prefill_scenes"] == {
        "condition_spec_direct_eval": 1,
        "laya_edge_decision": 1,
    }
    assert report["turn_plan"]["decisions"] == {"answer_only": 1, "start_new_task": 1}
    assert report["turn_plan"]["frame_kinds"] == {"conversation": 1, "sop": 1}


def test_collect_stats_edge_kind_distribution(conn) -> None:
    module = _load_script()
    report = module.collect_stats(conn, tenant_id=None, since=None, limit=1000)
    kinds = {(kind, status): count for kind, status, count in report["edge_conditions"]}
    assert kinds[("llm_judge", "compiled")] == 3
    assert kinds[("slots_all", "compiled")] == 7


def test_render_mentions_all_sections(conn) -> None:
    module = _load_script()
    report = module.collect_stats(conn, tenant_id=None, since=None, limit=1000)
    rendered = module.render(report)
    for section in ("[A] LLM operation", "[B] 知识检索路由", "[C] SOP 边条件", "[D] SOP 场景", "[E] TurnPlanner", "[F] 结论"):
        assert section in rendered
    assert "llm_judge 占比" in rendered
    assert "0.75" in rendered


def test_build_verdicts_summarises_opportunity(conn) -> None:
    module = _load_script()
    report = module.collect_stats(conn, tenant_id=None, since=None, limit=1000)
    verdicts = module.build_verdicts(report)
    assert len(verdicts) == 3
    # 只有 1 个 SOP 帧 → P1 判定为机会极小
    assert "机会极小" in verdicts[0]
    # llm_judge 占比 3/10
    assert "30.0%" in verdicts[0]
    assert "planner" in verdicts[1]
    assert "知识路由" in verdicts[2]