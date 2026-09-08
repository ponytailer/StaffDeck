from __future__ import annotations


# Control-plane routers already have deterministic fallbacks. Retrying an empty
# reasoning-only response delays the real work without improving routing.
OPERATION_EMPTY_RESPONSE_RETRIES: dict[str, int] = {
    "knowledge.document_route": 0,
    "knowledge.bucket_route": 0,
}

# JSON repair resends the full payload to the model. For long-context
# interactive loops (Harness decisions, intent planning) each extra attempt
# costs a complete LLM round trip; one repair attempt is enough because the
# caller has its own failure fallbacks. Generative stages keep the default.
OPERATION_JSON_REPAIR_ATTEMPTS: dict[str, int] = {
    "harness.task_action": 1,
    "turn_planner.plan": 1,
}


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
