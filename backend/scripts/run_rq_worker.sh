#!/usr/bin/env bash
# 启动定时任务 rq worker（独立常驻进程）。
#
# 前置：
#   - 与 FastAPI(single_port_app) 指向同一 DATABASE_URL 与 REDIS_HOST（读 backend/.env）；
#   - SCHEDULER_BACKEND=rq（默认）。
#
# 说明：
#   - 本进程负责「按点触发 + 执行」，FastAPI 进程只负责写时同步 / 启动全量同步；
#   - 需要并发就多起几个本进程（共享同一 Redis 队列），rq 单 worker 同时只跑一个 job；
#   - 生产用 supervisor/systemd 托管，保持常驻；本脚本可直接用于前台 / nohup。
#
# 用法：
#   backend/scripts/run_rq_worker.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$HERE/.." && pwd)"
cd "$BACKEND"

exec .venv/bin/python -m app.scheduled_tasks.rq_worker
