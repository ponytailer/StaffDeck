from __future__ import annotations


# Control-plane routers already have deterministic fallbacks. Retrying an empty
# reasoning-only response delays the real work without improving routing.
OPERATION_EMPTY_RESPONSE_RETRIES: dict[str, int] = {
    "knowledge.document_route": 0,
    "knowledge.bucket_route": 0,
    # 同上：抽取失败有模板兜底，空响应重试只会拉长尾延迟
    "sop.slot_extraction": 0,
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
