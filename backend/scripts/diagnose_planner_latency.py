"""只读诊断：拉最近打招呼 turn 的 llm_call_finished span，拆解判断意图耗时。

用法：cd backend && .venv/bin/python scripts/diagnose_planner_latency.py
只读 SELECT，不写任何数据。

说明：新代码中 planner/harness span 均带 turn_id，可精确归因；旧代码环境下
span 可能缺 turn_id，脚本会按时间窗口兜底匹配并标注 [窗口匹配]。

兼容性（2026-09-09 线上修复）：payload_json / created_at 在 PG 下返回
dict/datetime，在 sqlite 下可能返回 str，统一经 as_payload / as_datetime 归一。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db.database import engine  # noqa: E402


def as_payload(value: object) -> dict:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return value if isinstance(value, dict) else {}


def as_datetime(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) else None


# 服务器 DB 存 UTC；输出统一转北京时间（+8h）显示，避免人工换算看错
TZ_OFFSET = timedelta(hours=8)


def bj_ts(value: datetime | None) -> str:
    if value is None:
        return "?"
    return (value + TZ_OFFSET).strftime("%Y-%m-%d %H:%M:%S")


def bj_hms(value: datetime | None) -> str:
    if value is None:
        return "?"
    return (value + TZ_OFFSET).strftime("%H:%M:%S")


def _check_route_cache_support() -> None:
    """部署自检：路由缓存三项前提 + 最近 span 的新代码标志。

    ① REDIS_HOST 配置与连通性（未配置/连不上 = 缓存静默禁用，行为与旧代码一致）
    ② 新代码标志：最近 bucket_route span 是否带 payload_chars（瘦身版才记录）
    ③ 缓存命中迹象：最近 span 里 route_cache 相关 phase 出现情况
    """

    print("=" * 60)
    print("路由缓存/瘦身部署自检")
    print("=" * 60)

    # ① Redis 底座
    try:
        from app.redis_client import get_redis

        client = get_redis()
        if client is None:
            print("  [!] Redis：不可用（REDIS_HOST 未配置或连接失败）")
            print("      → 路由缓存静默禁用，行为与旧代码一致；重新启用需配置并重启进程")
        else:
            try:
                info = client.info("server")
                print(
                    "  [OK] Redis 已连通："
                    f"{info.get('redis_version', '?')} / keys≈{client.dbsize()}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [!] Redis client 存在但 ping/info 失败：{exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] Redis 检查异常：{exc}")

    # ②③ 扫最近 span 找新代码标志
    payload_chars_seen: list[tuple[str, int]] = []
    route_cache_hits = 0
    route_cache_stored = 0
    slim_spans = 0
    total_bucket_routes = 0
    planner_chars: list[tuple[str, int]] = []
    try:
        # 每段查询独立开连接：第一条查询若失败会把连接置为 invalid，
        # 复用同一连接的后续查询会报 "This Connection is closed"
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT payload_json, created_at
                    FROM agent_events
                    WHERE event_type = 'llm_call_finished'
                    ORDER BY created_at DESC
                    LIMIT 200
                    """
                )
            ).fetchall()
        for row in rows:
            payload = as_payload(row[0])
            # 新版 _select_buckets_with_llm 在 llm_operation 上挂 payload_chars；
            # llm_call_finished 的 payload 可能是嵌套 attributes 或平铺，两处都查
            flat_attrs = payload.get("attributes") if isinstance(payload.get("attributes"), dict) else payload
            chars = flat_attrs.get("payload_chars")
            op = str(payload.get("operation") or "?")
            if op == "knowledge.bucket_route":
                total_bucket_routes += 1
                if isinstance(chars, (int, float)):
                    slim_spans += 1
                    payload_chars_seen.append((bj_hms(as_datetime(row[1])), int(chars)))
            if op == "turn_planner.plan" and isinstance(chars, (int, float)):
                planner_chars.append((bj_hms(as_datetime(row[1])), int(chars)))
            trace_json = json.dumps(payload, ensure_ascii=False)
            if "route_cache_hit" in trace_json:
                route_cache_hits += 1
            if "route_cache_stored" in trace_json:
                route_cache_stored += 1

        if total_bucket_routes == 0:
            print("  [i] 最近 200 个 span 中无 bucket_route 调用，暂无法判断新代码是否生效")
        else:
            print(
                f"  bucket_route：最近 {total_bucket_routes} 次，其中带 payload_chars（新代码）{slim_spans} 次"
            )
            for ts, chars in payload_chars_seen[:5]:
                print(f"    [{ts}] payload_chars={chars}")
            if slim_spans == 0:
                print("  [!] 新代码未生效：所有 bucket_route span 均无 payload_chars → 检查部署/重启")
        print(
            f"  route_cache 迹象：最近 200 span 中 route_cache_hit ×{route_cache_hits}，"
            f"route_cache_stored ×{route_cache_stored}"
        )
        if route_cache_stored == 0 and route_cache_hits == 0 and total_bucket_routes > 0:
            print("  [!] 无任何 route_cache 迹象 → 大概率 Redis 未配置或缓存写入未发生")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] LLM span 扫描失败：{exc}")

    # 盲区补丁：缓存命中不发 LLM 调用 → llm_call_finished 里扫不到 hit。
    # route phase 经 harness_tool_completed 事件落库（tool_name=knowledge_search）。
    # 新代码在事件 payload.result.route_phases 带轻量 phase 列表；旧事件回退
    # 字符串匹配。独立连接 + 独立 try，前段失败不影响本段。
    ks_hits = ks_stores = ks_fast = 0
    ks_count = 0
    ks_old_style = 0
    try:
        with engine.connect() as conn:
            ks_rows = conn.execute(
                text(
                    """
                    SELECT payload_json, created_at
                    FROM agent_events
                    WHERE event_type = 'harness_tool_completed'
                    ORDER BY created_at DESC
                    LIMIT 150
                    """
                )
            ).fetchall()
        for row in ks_rows:
            payload = as_payload(row[0])
            # tool_name 在事件 payload 顶层（trace 链路平铺，不包一层）
            if str(payload.get("tool_name") or "") != "knowledge_search":
                continue
            ks_count += 1
            # 优先：新版事件 result.route_phases（轻量列表，精确计数）
            nested = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            phases = nested.get("route_phases")
            if isinstance(phases, list):
                ks_hits += sum(1 for p in phases if p == "route_cache_hit")
                ks_stores += sum(1 for p in phases if p == "route_cache_stored")
                ks_fast += sum(1 for p in phases if str(p).endswith("lexical_fast_path"))
            else:
                # 旧事件（无 route_phases）：result.data 可能还带完整 payload，
                # 字符串匹配做兜底，可能含 chunk 正文噪声
                trace_json = json.dumps(payload, ensure_ascii=False)
                ks_old_style += 1
                ks_hits += trace_json.count("route_cache_hit")
                ks_stores += trace_json.count("route_cache_stored")
                ks_fast += trace_json.count("lexical_fast_path")
        print(
            f"  知识检索事件：最近 {ks_count} 条（route_phases 精确 {ks_count - ks_old_style} /"
            f" 旧事件兜底 {ks_old_style}）→ route_cache_hit ×{ks_hits}，"
            f"route_cache_stored ×{ks_stores}，词法快速路径 ×{ks_fast}"
        )
        if ks_count == 0:
            print("  [i] 最近无 knowledge_search 调用事件；有检索但无事件 → 确认 trace_sink 配置")
        if ks_hits > 0:
            print("  [OK] 路由缓存命中已在发生（重复问法免 LLM 路由）")
        elif ks_count > 0 and ks_fast > 0:
            print("  [i] 无缓存命中但词法快速路径在生效（词法显著命中免 LLM 路由）")
        elif ks_count > 0 and ks_old_style == ks_count:
            print(
                "  [!] 事件均为旧格式（无 route_phases）→ 新 invoker 代码未部署，"
                "route phase 计数可能受 chunk 正文噪声干扰"
            )
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] 知识检索事件扫描失败：{exc}")

    # planner 瘦身观测（独立段，不依赖前段）
    try:
        if planner_chars:
            print(
                f"  planner 瘦身：最近 planner span 带 payload_chars {len(planner_chars)} 条"
                "（新代码；>25000 视为旧代码量级）"
            )
            for ts, chars in planner_chars[:5]:
                flag = "  ← 旧代码量级" if chars > 25_000 else ""
                print(f"    [{ts}] payload_chars={chars}{flag}")
        else:
            print("  planner 瘦身：最近 planner span 无 payload_chars → planner 瘦身代码未部署")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!] planner 观测失败：{exc}")
    print()


def main() -> None:
    _check_route_cache_support()
    with engine.connect() as conn:
        # 最近 8 个 user_message_received 事件 → 定位 turn
        rows = conn.execute(
            text(
                """
                SELECT session_id, payload_json, created_at
                FROM agent_events
                WHERE event_type = 'user_message_received'
                ORDER BY created_at DESC
                LIMIT 4
                """
            )
        ).fetchall()
        print(f"最近 {len(rows)} 条用户消息：")
        turns: list[tuple[str, str, datetime | None]] = []
        for row in rows:
            payload = as_payload(row[1])
            message = str(payload.get("message") or "")[:40]
            turn_id = str(payload.get("turn_id") or payload.get("message_id") or "")
            turn_created_at = as_datetime(row[2])
            shown = bj_ts(turn_created_at)
            print(f"  [{shown}] turn={turn_id[:16]}... msg={message!r}")
            turns.append((row[0], turn_id, turn_created_at))

        # 对每个 turn 拉全部 llm_call_finished span
        print("\n各 turn 的 LLM 调用明细：")
        for session_id, turn_id, turn_created_at in turns[:4]:
            # 注意行结构：(payload_json, created_at)
            spans = conn.execute(
                text(
                    """
                    SELECT payload_json, created_at
                    FROM agent_events
                    WHERE event_type = 'llm_call_finished'
                      AND session_id = :session_id
                    ORDER BY created_at DESC
                    LIMIT 120
                    """
                ),
                {"session_id": session_id},
            ).fetchall()
            exact: list[dict] = []
            window: list[tuple[dict, datetime]] = []
            for row in spans:
                payload = as_payload(row[0])
                created_at = as_datetime(row[1])
                if turn_id in (payload.get("turn_id"), payload.get("user_message_id")):
                    exact.append(payload)
                elif (
                    turn_created_at is not None
                    and created_at is not None
                    and turn_created_at <= created_at <= turn_created_at + timedelta(minutes=10)
                ):
                    # 旧代码 span 无 turn_id：按「本 turn 消息时间 → +10min」窗口兜底
                    window.append((payload, created_at))

            label = f"turn {turn_id[:16]}..."
            if not exact and not window:
                # spans 行结构为 (payload_json, created_at)
                recent_ops = [
                    str((p or {}).get("operation") or "?") for p, _ts in spans[:8]
                ]
                print(f"\n{label}  0 次匹配（该会话最近 span operations: {recent_ops}）")
                continue

            if exact:
                print(f"\n{label}  共 {len(exact)} 次模型调用（精确匹配）：")
                for payload in sorted(exact, key=lambda p: str(p.get("started_at") or "")):
                    print(
                        "  {op} model={model} dur={dur}ms json_attempt={ja}/{jm}".format(
                            op=str(payload.get("operation") or "?"),
                            model=str(payload.get("model_name") or payload.get("model") or "?"),
                            dur=payload.get("duration_ms"),
                            ja=payload.get("json_attempt"),
                            jm=payload.get("json_max_attempts"),
                        )
                    )
            if window:
                print(
                    f"\n{label}  窗口兜底匹配 {len(window)} 次"
                    "（旧代码 span 无 turn_id，含上一 turn 的后台任务，仅供参考）："
                )
                for payload, created_at in sorted(window, key=lambda item: item[1]):
                    print(
                        "  [{ts}] {op} model={model} dur={dur}ms json_attempt={ja}/{jm}".format(
                            ts=bj_hms(created_at),
                            op=str(payload.get("operation") or "?"),
                            model=str(payload.get("model_name") or payload.get("model") or "?"),
                            dur=payload.get("duration_ms"),
                            ja=payload.get("json_attempt"),
                            jm=payload.get("json_max_attempts"),
                        )
                    )


if __name__ == "__main__":
    main()
