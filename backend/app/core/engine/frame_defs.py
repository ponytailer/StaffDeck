"""TaskFrame 调度共享常量与异常（批次 4.5 自 frames.py 下沉，frames.py 保持 re-export）。"""

from __future__ import annotations


TERMINAL_FRAME_STATUSES = {"completed", "cancelled", "failed"}
DEPENDENCY_WAITING_ERROR_CODES = {
    "DEPENDENCY_BLOCKED",  # compatibility with the first Harness v2 scheduler
    "DEPENDENCY_WAITING",
}
HARNESS_CONTEXT_KEY = "harness_v2"
FRAME_LEASE_SECONDS = 900


class TaskFrameClaimConflict(RuntimeError):
    pass
