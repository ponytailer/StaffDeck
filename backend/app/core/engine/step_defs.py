"""Step 循环共享常量/类型别名/协议异常/HarnessAction（批次 4.6 自 step_loop.py 下沉）。

step_loop.py 保持 re-export；_deadline_llm_client 是 LLMClient 的唯一实例化点与测试打桩缝，
留在 step_loop。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, Field

from app import paths

PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "harness_agent_prompt.md"
MAX_SUCCESSFUL_KNOWLEDGE_SEARCHES_PER_TASK = 2
# 模型可见 tool result 的字符预算。知识检索走内联（精简载荷，预算内分级降档），
# 超出预算的部分由模型按 chunk_id 用 read_knowledge_chunk 取原文（I1）。
_CAPABILITY_RESULT_TRANSCRIPT_MAX_CHARS = 12_000
# I1（qa-optimization-plan 第一批）：知识结果内联预算 25K → 10K。
# 超预算走分级降档（KNOWLEDGE_RESULT_TIERED_INLINE），不再截成半截 JSON。
_KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS = 10_000
# I1 分级内联：evidence 前 N 条保留正文（各 ≤ 下一个常量），其余退化成定位索引。
# 回滚开关：置 False 恢复「直接截断 preview」的旧行为。
KNOWLEDGE_RESULT_TIERED_INLINE = True
_KNOWLEDGE_TIER_FULL_EVIDENCE = 3
_KNOWLEDGE_TIER_EVIDENCE_CONTENT_CHARS = 1_500

# 动作生成（harness.task_action）单次模型调用最多尝试几次。此前上游超时/限流会
# 直接终止整个 TaskFrame，把「瞬时不可用」当成「模型没按协议返回动作」，用户等了
# 数分钟后只拿到一句「当前任务的执行模型没有返回有效动作。」（2026-09-28 现场：
# 41K 字符 payload + 思考模式下第 14 轮撞 90s 上限）。一次有界重试即可覆盖网关抖动，
# 又不会让最坏延迟失控（每次尝试仍受 output_policy 的单次上限约束）。
_ACTION_MODEL_ATTEMPTS = 2
_MODEL_TRANSIENT_ERROR_CODES = frozenset(
    {
        "MODEL_TIMEOUT",
        "MODEL_RATE_LIMITED",
        "MODEL_UPSTREAM_UNAVAILABLE",
        "MODEL_UPSTREAM_ERROR",
    }
)
# 供 harness_v2_engine 判定 SOP 步骤是否可续跑：模型侧瞬时失败与协议失败一样
# 不应该吃掉已完成节点的结果。
MODEL_UNAVAILABLE_FAILURE_CODE = "HARNESS_MODEL_UNAVAILABLE"
# 面向用户的失败归因（截图/回复里只看到「没有返回有效动作」时无从判断该怎么办）。
_MODEL_FAILURE_REASONS = {
    "MODEL_TIMEOUT": "模型调用超时",
    "MODEL_RATE_LIMITED": "模型被限流",
    "MODEL_UPSTREAM_UNAVAILABLE": "模型上游服务不可用",
    "MODEL_UPSTREAM_ERROR": "模型上游返回错误",
    "MODEL_AUTHENTICATION_FAILED": "模型凭证无效",
    "MODEL_PERMISSION_DENIED": "模型权限不足",
    "MODEL_ENDPOINT_NOT_FOUND": "模型或接入地址不存在",
    "MODEL_INVALID_REQUEST": "模型拒绝了本次请求",
    "MODEL_REQUEST_TOO_LARGE": "请求体超过模型上限",
    "MODEL_UPSTREAM_CONFLICT": "模型上游冲突",
}
# 这些失败重发无用，必须改配置。
_MODEL_CONFIG_ERROR_CODES = frozenset(
    {
        "MODEL_AUTHENTICATION_FAILED",
        "MODEL_PERMISSION_DENIED",
        "MODEL_ENDPOINT_NOT_FOUND",
    }
)
ToolInvoker = Callable[[str, dict[str, Any]], dict[str, Any]]
TraceSink = Callable[[str, dict[str, Any]], None]
CancellationCheck = Callable[[], bool]


@lru_cache(maxsize=1)
def harness_system_prompt() -> str:
    """task_action 的 system prompt（I2）。

    返回值必须**逐字节稳定**：它是 prompt cache 的前缀，任何改动都会让上游
    缓存全量失效一次。协议约束等不变段写在提示词文件前部；当前能力清单、
    槽位、预算等每轮变化的内容只进 user payload，绝不拼进 system。
    """

    return PROMPT_PATH.read_text(encoding="utf-8").strip()

# 轻量节点直通（P1-1）：结构简单的 SOP 节点（唯一必执行能力 + 唯一无条件转移 +
# 无缺槽）的模型决策是低风险分类/编排任务，优先用意图识别轻量模型 + 一次性
# 编排提示，减少主模型串行调用；模型输出仍走同一校验协议，失败自动回退主模型。
LIGHTWEIGHT_DIRECTIVE = (
    "当前节点结构简单：只需调用一个必执行能力，成功后用 finish(completed) 收尾，"
    "并沿用唯一可用转移的 next_node_id。请直接输出 tool -> finish 动作序列，"
    "不要展开多余检查或重复确认。"
)


class HarnessExecutionCancelled(RuntimeError):
    pass


class HarnessExecutionFenced(RuntimeError):
    pass


class HarnessAction(BaseModel):
    action: Literal["tool", "finish"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: Literal["completed", "awaiting_user", "handoff", "failed"] | None = None
    reply_fragment: str = ""
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    next_step_id: str | None = None
    task_summary: str = ""
    structured_result: Any | None = None
    # A2UI：缺槽询问的表单描述（确定性执行器产出），原样带到消息 metadata。
    ui_form: dict[str, Any] | None = None

