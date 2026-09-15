"""一次性探针：看最近助手消息 metadata_json 里到底有没有 a2ui。

用法：.venv/bin/python scripts/_probe_a2ui_message.py <内容关键字|session_id 前缀> [limit]
关键字带 ``%`` 时按 session_id LIKE 查，否则按内容 ILIKE 查。
"""

from __future__ import annotations

import json
import sys

from sqlalchemy import create_engine, text

from app.config import get_settings


def _meta(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:  # noqa: BLE001
            return {"_raw": str(value)[:200]}
    return value if isinstance(value, dict) else {}


def main() -> None:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "在职证明"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    by_session = "%" in keyword
    engine = create_engine(get_settings().database_url)
    sql = (
        "SELECT id, session_id, role, created_at, content, metadata_json "
        "FROM messages WHERE "
        + ("session_id LIKE :kw" if by_session else "content ILIKE :kw OR metadata_json::text ILIKE '%a2ui%'")
        + " ORDER BY created_at DESC LIMIT :lim"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"kw": keyword, "lim": limit}).fetchall()
        for row in rows:
            meta = _meta(row.metadata_json)
            print(f"[{row.created_at}] {row.role:9s} id={str(row.id)[:12]} session={str(row.session_id)[:14]}")
            print(f"    content: {str(row.content)[:160]!r}")
            print(f"    meta keys: {sorted(meta.keys())}")
            if "a2ui" in meta:
                print(f"    a2ui: {json.dumps(meta['a2ui'], ensure_ascii=False)[:800]}")
            print()


if __name__ == "__main__":
    main()
