from __future__ import annotations


# Control-plane routers already have deterministic fallbacks. Retrying an empty
# reasoning-only response delays the real work without improving routing.
OPERATION_EMPTY_RESPONSE_RETRIES: dict[str, int] = {
    "knowledge.document_route": 0,
    "knowledge.bucket_route": 0,
    # 同上：抽取失败有模板兜底，空响应重试只会拉长尾延迟
    "sop.slot_extraction": 0,
    # 入库两阶段：空响应重试会**整包重发**（bucketing 的 payload 就有 ~21K 字符），
    # 重试一次就是又一轮分钟级等待。两者都有确定性降级路径（见 JSON_REPAIR 注释）。
    # 不关掉这一项的话，重试轮数会被放大成 (json_repair+1)×(empty+1) 倍。
    "knowledge.ingest_bucket": 0,
    "knowledge.discovery": 0,
}

# JSON repair resends the full payload to the model. For long-context
# interactive loops (Harness decisions, intent planning) each extra attempt
# costs a complete LLM round trip; one repair attempt is enough because the
# caller has its own failure fallbacks. Generative stages keep the default.
OPERATION_JSON_REPAIR_ATTEMPTS: dict[str, int] = {
    "harness.task_action": 1,
    "turn_planner.plan": 1,
    # SOP 槽位抽取是小 payload 控制面调用，失败直接退回模板询问；
    # 慢模型上一次修复重试就是十几秒，不值得（2026-09-09 实测抽取
    # 平均 40s 主要来自 json_attempt 串行重试）。
    "sop.slot_extraction": 0,
    # 边条件离线编译（rq 异步）：一次编译覆盖整张图的条件边，失败保持自由文本
    # 即可（运行时回落 LLM），多轮修 JSON 只是白烧 token。
    "sop.edge_condition_compile": 1,
    # 文档入库（2026-09-14 实测）：bucketing payload ~21K 字符、discovery ~17K 字符，
    # 暖态单次 66s / 115s，网关抖动时可到分钟级。而默认预算是 4 次 JSON 重发 ×
    # 3 次空响应重试 = 最多 16 轮整包重发，且此前**没有超时上限**（吃 600s 默认值），
    # 最坏情况能把一次入库从 3 分钟拖到 10 分钟以上——用户只看到三个计数长期为 0，
    # 误判「文档处理失败」并删库重传（真实发生）。
    # bucketing 保留 1 次修复：失败会退化成纯结构化桶（主题变粗），质量损失值得再试一次。
    # discovery 只产出「可确认的 SOP/工具建议」，失败直接跳过，不值得重试。
    "knowledge.ingest_bucket": 1,
    "knowledge.discovery": 0,
}


# Interactive control-plane calls sit on the chat turn's critical path. When
# the upstream gateway hangs, a call would otherwise block for the full model
# timeout (default 600s) before the caller's fallback can engage — observed as
# an 8+ minute silent stall (2026-09-09). Clamp each interactive operation to a
# bounded ceiling; every one of these has a deterministic/LLM fallback.
OPERATION_TIMEOUT_SECONDS: dict[str, float] = {
    "turn_planner.plan": 90.0,
    "harness.task_action": 90.0,
    "knowledge.document_route": 60.0,
    "knowledge.bucket_route": 60.0,
    "sop.slot_extraction": 30.0,
    "session.title": 30.0,
    "memory.capture": 30.0,
    # 离线编译跑在 rq worker 里、不在对话关键路径上，可以给足时间；但必须有
    # 上限，否则上游网关挂死会把整个 worker 卡住（rq 单 worker 串行消费）。
    "sop.edge_condition_compile": 180.0,
    # 文档入库：这两阶段此前**完全没有上限**，直接吃 model_api_timeout_seconds
    # 的 600s 默认值——上游网关一旦挂住，一次入库就是 10 分钟以上无任何反馈
    # （2026-09-14 实测两个真实入库任务 573s / 635s）。
    # 上限之内完全够用：暖态实测 bucketing 66s、discovery 115s。
    # 超时后的降级是安全且完整的：bucketing 退回「按章节结构」建桶（仍会生成
    # 切片与知识页，只是主题更粗），discovery 本就可选（只影响 SOP/工具建议）。
    "knowledge.ingest_bucket": 120.0,
    "knowledge.discovery": 90.0,
}


def operation_timeout_seconds(operation: str | None, default_seconds: float) -> float:
    configured = OPERATION_TIMEOUT_SECONDS.get(str(operation or ""))
    if configured is None:
        return max(1.0, float(default_seconds))
    return max(1.0, float(configured))


def operation_json_repair_attempts(operation: str, default_attempts: int) -> int:
    configured = OPERATION_JSON_REPAIR_ATTEMPTS.get(operation)
    return max(0, int(default_attempts if configured is None else configured))

def operation_output_tokens(operation: str, configured_tokens: int) -> int:
    """Return the operator-configured output budget without hidden overrides.

    ``operation`` remains part of the API so callers and observability can keep
    identifying the stage, but every stage now uses the model configuration's
    Max Tokens value as its single source of truth.
    """

    del operation
    return max(1, int(configured_tokens or 1))


def operation_empty_response_retries(operation: str, default_retries: int) -> int:
    configured = OPERATION_EMPTY_RESPONSE_RETRIES.get(operation)
    return max(0, int(default_retries if configured is None else configured))
