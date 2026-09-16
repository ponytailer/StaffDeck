"""一次性探针：C2/prefetch 触发情况 + 14:22 检索归属 + 确认 turn 链路。"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from sqlalchemy import create_engine, text

from app.config import get_settings


def bj(value: datetime | None) -> str:
    if value is None:
        return "?"
    return value.strftime("%m-%d %H:%M:%S")


def payload(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(str(value) or "{}")
    except Exception:  # noqa: BLE001
        return {}


def main() -> None:
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        print("=== ① 今日 sop_prefill_planned 场景分布（最近 30 条） ===")
        rows = conn.execute(
            text(
                """
                SELECT payload_json, created_at, session_id
                FROM agent_events
                WHERE event_type = 'sop_prefill_planned'
                ORDER BY created_at DESC
                LIMIT 30
                """
            )
        ).fetchall()
        scenes: dict[str, int] = {}
        for row in rows:
            data = payload(row[0])
            scene = str(data.get("scene") or "?")
            scenes[scene] = scenes.get(scene, 0) + 1
            if "knowledge" in scene:
                print(f"  [hit] {bj(row[1])} scene={scene} session={row[2]}")
        print("  scene 分布:", scenes)

        print("\n=== ② 14:22:56 knowledge_search 的归属（前后 90s 的同会话事件） ===")
        row = conn.execute(
            text(
                """
                SELECT payload_json, created_at, session_id
                FROM agent_events
                WHERE event_type = 'harness_tool_completed'
                AND payload_json::text LIKE '%年假政策%'
                ORDER BY created_at DESC
                LIMIT 3
                """
            )
        ).fetchall()
        target_session = None
        for r in row:
            data = payload(r[0])
            print(f"  ts={bj(r[1])} session={r[2]} query={str(data.get('query'))[:50]!r}")
            target_session = r[2]
        if target_session:
            ctx = conn.execute(
                text(
                    """
                    SELECT event_type, payload_json, created_at
                    FROM agent_events
                    WHERE session_id = :sid
                    AND created_at BETWEEN :t0 AND :t1
                    ORDER BY created_at ASC
                    """
                ),
                {
                    "sid": target_session,
                    "t0": row[0][1].replace(second=0) if row else None,
                    "t1": row[0][1] if row else None,
                },
            ).fetchall()
            for r in ctx:
                data = payload(r[1])
                brief = str(data.get("scene") or data.get("operation") or data.get("tool_name") or "")[:40]
                print(f"    {bj(r[2])} {r[0]} {brief}")

        print("\n=== ③ '确认：确认' turn（14:23:55–14:24:30）事件链 ===")
        rows3 = conn.execute(
            text(
                """
                SELECT event_type, payload_json, created_at, session_id
                FROM agent_events
                WHERE created_at BETWEEN '2026-09-15 14:23:50' AND '2026-09-15 14:24:30'
                AND event_type IN (
                    'sop_prefill_planned', 'sop_slot_extraction', 'harness_tool_completed',
                    'llm_call_finished', 'harness_action_failed'
                )
                ORDER BY created_at ASC
                LIMIT 40
                """
            )
        ).fetchall()
        for r in rows3:
            data = payload(r[1])
            op = str(data.get("operation") or data.get("scene") or data.get("tool_name") or "")
            extra = ""
            if r[0] == "sop_slot_extraction":
                extra = f" extracted={data.get('extracted_count')}/{data.get('total_count')}"
            if r[0] == "llm_call_finished":
                extra = f" dur={data.get('duration_ms')}ms"
            print(f"  {bj(r[2])} {r[0]} {op[:36]}{extra} session={str(r[3])[-8:]}")


if __name__ == "__main__":
    sys.exit(main())
