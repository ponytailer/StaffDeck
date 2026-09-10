"""本地开发 / 运维便捷入口（由 ``pyproject.toml`` 的 ``[project.scripts]`` 暴露）。

用法::

    uv run serve                  # 启动后端一体化服务（默认 127.0.0.1:5173 + 热重载）
    uv run serve --port 9000      # 换端口
    uv run serve --no-reload      # 关闭热重载（线上/调试用）
    uv run rq-worker              # 启动定时任务 rq worker 独立进程

等价的原命令::

    uv run uvicorn single_port_app:app --host 127.0.0.1 --port 5173 \\
        --log-config log.json --reload
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5173
_APP_MODULE = "single_port_app:app"

# app/cli.py → 上一级即后端工程根（log.json 所在处）
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_CONFIG = _BACKEND_ROOT / "log.json"


def _resolve_log_config(explicit: str | None) -> str | None:
    """解析日志配置路径，统一转成绝对路径。

    相对路径按后端工程根解析——否则从仓库根执行 ``uv run serve`` 时
    uvicorn 会找不到 ``log.json``（原命令依赖「先 cd 到 backend」）。
    文件不存在则返回 None，退回 uvicorn 默认日志配置。
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = _BACKEND_ROOT / candidate
        candidate = candidate.resolve()
        return str(candidate) if candidate.exists() else None
    return str(_DEFAULT_LOG_CONFIG) if _DEFAULT_LOG_CONFIG.exists() else None


def serve(argv: list[str] | None = None) -> None:
    """启动 FastAPI 一体化服务（后端 API + 前端 dist 同进程）。"""
    parser = argparse.ArgumentParser(
        prog="serve",
        description="启动 StaffDeck 后端一体化服务（uvicorn）",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("STAFFDECK_HOST", DEFAULT_HOST),
        help=f"监听地址（默认 {DEFAULT_HOST}，可用 STAFFDECK_HOST 覆盖）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STAFFDECK_PORT", DEFAULT_PORT)),
        help=f"监听端口（默认 {DEFAULT_PORT}，可用 STAFFDECK_PORT 覆盖）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并发进程数",
    )
    parser.add_argument(
        "--log-config",
        default=None,
        help="日志配置 JSON（默认 backend/log.json）",
    )
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=False,
        help="开启代码热重载（默认开启）",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="关闭热重载",
    )
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        _APP_MODULE,
        host=args.host,
        port=args.port,
        workers=args.workers,
        log_config=_resolve_log_config(args.log_config),
        reload=args.reload,
    )


if __name__ == "__main__":  # pragma: no cover
    serve()
