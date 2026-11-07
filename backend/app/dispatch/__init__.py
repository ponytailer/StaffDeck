"""调度入口（Dispatcher）：一种只做路由的 Agent 类型。

对外只暴露 service / router / stream 三个模块：
- ``service``：成员 CRUD、上下文拼装、模型解析
- ``router``：路由决策（LLM + 归一化）
- ``stream``：同步内联委派 + relay 镜像

命名务必用 dispatch / dispatcher，**不要叫 Router** ——
``app/core/plan/router.py`` 已有 SOP 场景路由的 ``Router`` 类，撞名会误导。
"""

from __future__ import annotations

from app.dispatch.schema import (
    DEFAULT_CLARIFY_TEXT,
    DEFAULT_FALLBACK_REPLY,
    MAX_DISPATCH_MEMBERS,
    MIN_DISPATCH_MEMBERS,
    DispatchConfig,
    DispatchRouteDecision,
    DispatcherCreateRequest,
    DispatcherMemberInput,
    DispatcherMemberRead,
    DispatcherRead,
    DispatcherUpdateRequest,
)

__all__ = [
    "DEFAULT_CLARIFY_TEXT",
    "DEFAULT_FALLBACK_REPLY",
    "MAX_DISPATCH_MEMBERS",
    "MIN_DISPATCH_MEMBERS",
    "DispatchConfig",
    "DispatchRouteDecision",
    "DispatcherCreateRequest",
    "DispatcherMemberInput",
    "DispatcherMemberRead",
    "DispatcherRead",
    "DispatcherUpdateRequest",
]