"""只读诊断：Laya 决策头在「SOP 走向」与「知识路由」两处的可替代机会。

用途：在决定是否继续投入 Laya（场景 L / 知识路由 P2'）之前，先用真实数据量：
  1. 知识检索里「真正走了 LLM 路由（document_route / bucket_route）」的比例与耗时；
  2. SOP 边条件的编译分布（llm_judge 占比 = 场景 L 的机会上限）；
  3. SOP 场景直判现状（sop_prefill_planned 各 scene）；
  4. TurnPlanner 里 answer_only 占比与 planner 实际调 LLM 的比例（P2 上限）。

用法：
    cd backend && .venv/bin/python scripts/diagnose_laya_opportunity.py
    cd backend && .venv/bin/python scripts/diagnose_laya_opportunity.py --days 7
    cd backend && .venv/bin/python scripts/diagnose_laya_opportunity.py --tenant tenant_demo
    cd backend && .venv/bin/python scripts/diagnose_laya_opportunity.py --json > report.json

只读 SELECT，不写任何数据。数据源：`agent_events`（PG 下 payload_json 是 JSON，
sqlite 下可能是 TEXT，统一经 `as_payload` 归一）+ `skill_edge_conditions`。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

# 只拉需要的事件类型，避免全表扫描
EVENT_TYPES = (
    "llm_call_finished",
    "harness_tool_completed",
    "sop_prefill_planned",
    "turn_plan_created",
)

# 知识路由里最贵、也最适合 Laya 的两个 operation
ROUTE_OPERATIONS = ("knowledge.document_route", "knowledge.bucket_route")


def as_payload(value: object) -> dict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def percentiles(values: list[float], ps: tuple[int, ...] = (50, 90, 99)) -> dict[int, float]:
    if not values:
        return {p: 0.0 for p in ps}
    ordered = sorted(values)
    out: dict[int, float] = {}
    for p in ps:
        # 最近秩法（nearest-rank）：小样本下不插值出不存在的时间
        rank = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
        out[p] = round(ordered[rank], 1)
    return out


def categorize_route(phases: list[str]) -> str:
    """按优先级给一次 knowledge_search 的路由结果分类（近似，见模块说明）。

    注意：`document_route` / `bucket_route` 只是「进入该路由步骤」的标记，
    不代表一定调了 LLM；真正跳过 LLM 的信号是 cache_hit / shortcut / fast_path。
    """

    marks = set(phases)
    if any(p.endswith("cache_hit") or p.endswith("cache_hit_empty") for p in marks):
        return "route_cache_hit"
    if any(p.endswith("small_candidate_shortcut") for p in marks):
        return "small_candidate_shortcut"
    if any(p.endswith("lexical_fast_path") for p in marks):
        return "lexical_fast_path"
    if any(p.endswith("lexical_fallback") for p in marks):
        return "llm_route_failed_fallback"
    if any(p.endswith("_lexical") for p in marks):
        return "lexical_only_no_model"
    if "document_route" in marks or "bucket_route" in marks:
        return "llm_route"
    if "okf_only" in marks:
        return "okf_only"
    if any(p.endswith("_no_match") for p in marks):
        return "no_match"
    if "no_visible_knowledge" in marks:
        return "no_visible_knowledge"
    if "no_documents" in marks:
        return "no_documents"
    return "other"


def fetch_events(conn, *, tenant_id: str | None, since: datetime | None, limit: int):
    where = [f"event_type IN ({', '.join(repr(t) for t in EVENT_TYPES)})"]
    params: dict[str, object] = {"limit": limit}
    if tenant_id:
        where.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if since is not None:
        where.append("created_at >= :since")
        params["since"] = since
    sql = (
        "SELECT event_type, payload_json FROM agent_events "
        f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT :limit"
    )
    return conn.execute(text(sql), params).all()


def edge_kind_distribution(conn, tenant_id: str | None) -> list[tuple[str, str, int]]:
    where = ""
    params: dict[str, object] = {}
    if tenant_id:
        where = "WHERE tenant_id = :tenant_id"
        params["tenant_id"] = tenant_id
    try:
        rows = conn.execute(
            text(
                "SELECT kind, status, count(*) FROM skill_edge_conditions "
                f"{where} GROUP BY kind, status ORDER BY count(*) DESC"
            ),
            params,
        ).all()
    except Exception:
        # 老库可能没有这张表；诊断脚本不因此失败
        return []
    return [(str(r[0] or ""), str(r[1] or ""), int(r[2] or 0)) for r in rows]


def table_overview(conn) -> dict:
    """agent_events 全表概览：判断窗口是否代表性（样本量/时间跨度/租户数）。"""

    try:
        row = conn.execute(
            text(
                "SELECT count(*), min(created_at), max(created_at), "
                "count(distinct tenant_id) FROM agent_events"
            )
        ).one()
    except Exception:
        return {}
    return {
        "total_events": int(row[0] or 0),
        "min_created_at": str(row[1]) if row[1] is not None else None,
        "max_created_at": str(row[2]) if row[2] is not None else None,
        "tenants": int(row[3] or 0),
    }


def collect_stats(conn, *, tenant_id: str | None = None, since: datetime | None = None, limit: int = 200_000) -> dict:
    events = fetch_events(conn, tenant_id=tenant_id, since=since, limit=limit)

    llm_durations: dict[str, list[float]] = {}
    knowledge_searches = 0
    knowledge_search_failed = 0
    route_categories: dict[str, int] = {}
    prefills: dict[str, int] = {}
    plan_decisions: dict[str, int] = {}
    plan_frame_kinds: dict[str, int] = {}

    for row in events:
        event_type = str(row[0])
        payload = as_payload(row[1])

        if event_type == "llm_call_finished":
            operation = str(payload.get("operation") or "llm.request")
            try:
                duration = float(payload.get("duration_ms") or 0.0)
            except (TypeError, ValueError):
                duration = 0.0
            llm_durations.setdefault(operation, []).append(duration)
            continue

        if event_type == "harness_tool_completed":
            if str(payload.get("tool_name") or "") != "knowledge_search":
                continue
            knowledge_searches += 1
            if not payload.get("success"):
                knowledge_search_failed += 1
            result = payload.get("result")
            phases = result.get("route_phases") if isinstance(result, dict) else None
            category = categorize_route([str(p) for p in phases]) if isinstance(phases, list) else "no_phases"
            route_categories[category] = route_categories.get(category, 0) + 1
            continue

        if event_type == "sop_prefill_planned":
            scene = str(payload.get("scene") or "?")
            prefills[scene] = prefills.get(scene, 0) + 1
            continue

        if event_type == "turn_plan_created":
            decision = str(payload.get("decision") or "?")
            plan_decisions[decision] = plan_decisions.get(decision, 0) + 1
            frames = payload.get("task_frames")
            if isinstance(frames, list):
                for frame in frames:
                    kind = str((frame or {}).get("kind") or "?") if isinstance(frame, dict) else "?"
                    plan_frame_kinds[kind] = plan_frame_kinds.get(kind, 0) + 1
            continue

    route_llm_calls = sum(len(llm_durations.get(op, [])) for op in ROUTE_OPERATIONS)
    llm_route_ratio = round(route_llm_calls / knowledge_searches, 3) if knowledge_searches else 0.0

    llm_table = {
        operation: {
            "count": len(durations),
            "p50": percentiles(durations).get(50, 0.0),
            "p90": percentiles(durations).get(90, 0.0),
            "p99": percentiles(durations).get(99, 0.0),
            "total_ms": round(sum(durations), 1),
        }
        for operation, durations in sorted(
            llm_durations.items(), key=lambda item: -len(item[1])
        )
    }

    return {
        "window": {
            "tenant_id": tenant_id,
            "since": since.isoformat() if since else None,
            "events": len(events),
            "limit": limit,
        },
        "llm_operations": llm_table,
        "overview": table_overview(conn),
        "knowledge": {
            "searches": knowledge_searches,
            "failed": knowledge_search_failed,
            "route_llm_calls": route_llm_calls,
            "route_llm_calls_per_search": llm_route_ratio,
            "route_categories": dict(sorted(route_categories.items(), key=lambda i: -i[1])),
        },
        "sop_prefill_scenes": dict(sorted(prefills.items(), key=lambda i: -i[1])),
        "turn_plan": {
            "decisions": dict(sorted(plan_decisions.items(), key=lambda i: -i[1])),
            "frame_kinds": dict(sorted(plan_frame_kinds.items(), key=lambda i: -i[1])),
        },
        "edge_conditions": edge_kind_distribution(conn, tenant_id),
    }


def _fmt_row(label: str, stats: dict, width: int = 30) -> str:
    return (
        f"{label:<{width}} {stats['count']:>7}  "
        f"{stats['p50']:>8} {stats['p90']:>8} {stats['p99']:>8}"
    )


def build_verdicts(report: dict) -> list[str]:
    """把数字翻成结论，避免使用者自己再推一遍。"""

    edges = report["edge_conditions"]
    total_edges = sum(count for _k, _s, count in edges)
    llm_judge = sum(count for kind, _s, count in edges if kind == "llm_judge")
    judge_share = llm_judge / total_edges if total_edges else 0.0
    sop_frames = report["turn_plan"]["frame_kinds"].get("sop", 0)

    plan_calls = report["llm_operations"].get("turn_planner.plan", {}).get("count", 0)
    answer_only = report["turn_plan"]["decisions"].get("answer_only", 0)
    turns = sum(report["turn_plan"]["decisions"].values())
    planner_ratio = plan_calls / turns if turns else 0.0

    knowledge = report["knowledge"]

    verdicts = [
        (
            f"P1（SOP 场景 L，已实现）：llm_judge 边占比 {judge_share:.1%}，"
            f"但窗口内 SOP 帧仅 {sop_frames} 个 → "
            + (
                "机会极小，建议保持关闭、不继续投入"
                if sop_frames < 10
                else "需再按节点访问频次细算"
            )
        ),
        (
            f"P2（planner SOP 选择）：{turns} 轮规划里 planner 实际调 LLM "
            f"{plan_calls} 次（{planner_ratio:.0%}），其中 answer_only {answer_only} 轮 → "
            + (
                "planner 仍频繁调用，P2 可能有省面"
                if planner_ratio >= 0.5
                else "大部分已被现有 fast path 覆盖，收益低"
            )
        ),
        (
            f"P2'（知识路由）：{knowledge['searches']} 次检索 / "
            f"{knowledge['route_llm_calls']} 次 LLM 路由调用"
            f"（{knowledge['route_llm_calls_per_search']}/次检索）→ "
            "Laya 唯一“每次发生、且是纯分类”的稳定落点"
            if knowledge["searches"]
            else "P2'（知识路由）：窗口内无检索数据"
        ),
    ]
    return verdicts


def render(report: dict) -> str:
    lines: list[str] = []
    window = report["window"]
    overview = report.get("overview") or {}
    lines.append("=" * 78)
    lines.append("Laya 机会诊断（只读）")
    lines.append(
        f"窗口：tenant={window['tenant_id'] or '(全部)'}  since={window['since'] or '(全部)'}  "
        f"扫描事件={window['events']}"
    )
    if overview:
        lines.append(
            f"全表：agent_events={overview.get('total_events')} 行  "
            f"{overview.get('min_created_at')} ~ {overview.get('max_created_at')}  "
            f"租户数={overview.get('tenants')}"
        )
    lines.append("=" * 78)

    lines.append("")
    lines.append("[A] LLM operation 耗时（ms）—— 谁最贵")
    lines.append(f"{'operation':<30} {'calls':>7}  {'p50':>8} {'p90':>8} {'p99':>8}")
    for operation, stats in report["llm_operations"].items():
        lines.append(_fmt_row(operation, stats))

    knowledge = report["knowledge"]
    lines.append("")
    lines.append("[B] 知识检索路由（P2' 机会）")
    lines.append(f"knowledge_search 次数                {knowledge['searches']:>7}")
    lines.append(f"  其中失败                            {knowledge['failed']:>7}")
    for operation in ROUTE_OPERATIONS:
        stats = report["llm_operations"].get(operation)
        if stats:
            lines.append(f"  {operation:<34} {stats['count']:>7}  P50={stats['p50']}ms")
    lines.append(
        f"LLM 路由调用 / 检索次数 ≈ {knowledge['route_llm_calls_per_search']} "
        f"(= {knowledge['route_llm_calls']} / {knowledge['searches']})"
    )
    lines.append("  每次检索的路由结果分类（近似）：")
    for category, count in knowledge["route_categories"].items():
        lines.append(f"    {category:<32} {count:>7}")

    lines.append("")
    lines.append("[C] SOP 边条件编译分布（场景 L 的机会上限）")
    if report["edge_conditions"]:
        lines.append(f"{'kind':<20} {'status':<12} {'count':>7}")
        for kind, status, count in report["edge_conditions"]:
            lines.append(f"{kind:<20} {status:<12} {count:>7}")
        llm_judge = sum(c for k, _s, c in report["edge_conditions"] if k == "llm_judge")
        total = sum(c for _k, _s, c in report["edge_conditions"])
        share = round(llm_judge / total, 3) if total else 0.0
        lines.append(f"→ llm_judge 占比 {share}（= {llm_judge} / {total}）")
    else:
        lines.append("  （无数据 / 表不存在）")

    lines.append("")
    lines.append("[D] SOP 场景直判现状（sop_prefill_planned）")
    if report["sop_prefill_scenes"]:
        for scene, count in report["sop_prefill_scenes"].items():
            lines.append(f"  {scene:<34} {count:>7}")
    else:
        lines.append("  （无数据）")

    lines.append("")
    lines.append("[E] TurnPlanner（P2 上限）")
    plan = report["turn_plan"]
    for decision, count in plan["decisions"].items():
        lines.append(f"  decision={decision:<26} {count:>7}")
    for kind, count in plan["frame_kinds"].items():
        lines.append(f"  frame.kind={kind:<24} {count:>7}")

    lines.append("")
    lines.append("[F] 结论")
    for verdict in build_verdicts(report):
        lines.append(f"  · {verdict}")

    lines.append("")
    lines.append("提示：")
    lines.append("  · [B] 的 route_llm_calls_per_search 越高 → 知识路由越值得接 Laya。")
    lines.append("  · [C] 的 llm_judge 占比越高 → 场景 L（已实现）收益越大。")
    lines.append("  · [E] 的 answer_only 占比越高 → P2（planner SOP 选择）的省面越大。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Laya 决策头机会诊断（只读）")
    parser.add_argument("--days", type=float, default=14.0, help="回溯天数（默认 14）")
    parser.add_argument("--tenant", default="", help="只统计某租户")
    parser.add_argument("--limit", type=int, default=200_000, help="最多扫描的事件数")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
    from app.db.database import engine  # 延迟导入，便于单测直接复用 collect_stats

    try:
        with engine.connect() as conn:
            report = collect_stats(
                conn,
                tenant_id=args.tenant or None,
                since=since,
                limit=args.limit,
            )
    except Exception as exc:  # noqa: BLE001 - 诊断脚本给出可读错误即可
        print(f"连接/查询失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("提示：确认 DATABASE_URL 可达（该脚本只读，不改数据）。", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())