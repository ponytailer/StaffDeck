"""诊断：rq worker 的连接在长时间阻塞读时会不会被 socket_timeout 打断。

不触碰真实队列（用不存在的 key），只测量 blmove 阻塞读的存活时间。
"""
from __future__ import annotations

import sys
import time

import redis

from app.config import get_settings

settings = get_settings()
mode = sys.argv[1] if len(sys.argv) > 1 else "short"
socket_timeout = 1.5 if mode == "short" else None

conn = redis.Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    password=settings.redis_password or None,
    socket_connect_timeout=1.5,
    socket_timeout=socket_timeout,
    decode_responses=False,
)
conn.ping()
print(f"mode={mode} socket_timeout={socket_timeout} redis={settings.redis_host}:{settings.redis_port}/{settings.redis_db}")

start = time.monotonic()
try:
    # 与 rq Queue.lmove 完全同形的调用（BLMOVE key tmp LEFT RIGHT timeout）
    conn.blmove("staffdeck_probe:definitely-empty-list", "staffdeck_probe:tmp", 20)
    print(f"BLMOVE 正常返回 None，耗时 {time.monotonic() - start:.2f}s")
except Exception as exc:  # noqa: BLE001
    print(f"BLMOVE 异常 {type(exc).__module__}.{type(exc).__name__}: {exc}，耗时 {time.monotonic() - start:.2f}s")
finally:
    conn.delete("staffdeck_probe:tmp")
