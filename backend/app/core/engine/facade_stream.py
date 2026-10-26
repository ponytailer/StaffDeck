"""AgentLoop 流式发射基础设施：节流常量、身份/提示辅助、TurnStreamSink。

批次 4.7 自 facade.py 搬出，零行为改动；facade.py 保持 re-export。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from time import sleep
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    model_for_agent,
    visible_published_skills,
    visible_skill,
)
from app.agents.free_model import resolve_turn_default_model
from app.agents.usage import bump_agent_usage
from app.chat_pubsub import publish_relay_wake
from app.core.converse.identity import AgentIdentityPrompt
from app.core.base.cancellation import clear_chat_turn_cancelled
from app.core.base.conversation_context import (
    ConversationContextSettings,
    build_conversation_context,
)
from app.core.base.conversation_projection import ConversationProjection
from app.core.base.graph_rules import GraphRules
from app.core.engine.step_loop import HarnessExecutionCancelled
from app.core.engine.session import HarnessSessionBusy
from app.core.engine.session import HarnessTurnConflict
from app.core.engine.turn import (
    HarnessV2Engine,
    _with_recoverable_first_session,
    get_or_create_harness_session,
)
from app.core.engine.handoff import HumanHandoffService
from app.core.converse.response import (
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.engine.skill_runtime import SkillRuntime
from app.core.plan.slash import SlashCommandError
from app.core.engine.finalizer import TurnFinalizer
from app.core.base.ui_flags import knowledge_citations_hidden
from app.db.models import (
    AgentProfile,
    ChatSession,
    HarnessTurnRecord,
    HumanHandoffRequest,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    UIConfig,
    new_id,
    utc_now,
)
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    restore_truncated_atomic_references,
    strip_citation_markers,
)
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    resolve_model_config_for_runtime,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService
from app.observability import EventLog
from app.observability.spans import llm_operation
from app.session.helpers import public_session
from app.session.message_visibility import visible_message_content, visible_message_rows
from app.session.origin import PILOTDECK_GROUP_CHAT_CHANNEL
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    RouterDecision,
    StepAgentResult,
)
from app.tools.tool_schema import ToolResult

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

STREAM_CHUNK_INTERVAL_SECONDS = 0.045
STREAM_CHUNK_SIZE = 8
# 真流式增量的落库节流：模型碎片可能来得远快于人的阅读节奏，逐片写库 + 推送
# 会把 relay 变成高频小包。合并到 60ms 一档再落库，既保留「随到随显」的观感，
# 又把写库次数压到 ~16 次/秒以内；合并只影响可见节奏，不拖慢本轮完成时间。
STREAM_DELTA_FLUSH_INTERVAL_SECONDS = 0.06
STREAM_DELTA_FLUSH_MAX_CHARS = 400
# 每一次落库都是一次 DB 往返（生产环境为远程 PG），长回答若始终按 60ms 一档
# 落库，写库耗时会被累加进本轮收尾。这里给整段生成一个写库次数上限，超过后
# 按已流逝时间反推间隔，把写库次数锁在常数级。
STREAM_DELTA_MAX_FLUSHES = 150
# 兜底打字机预算：没有真流式可用的轮次（直出回复、固定话术）按整轮最多多花
# 2.5 秒铺开，取代原先「一次性暴发」。
STREAM_TYPING_BUDGET_SECONDS = 2.5
MAX_TOOL_ACTIONS_PER_TURN = 32
MAX_TOOL_ACTIONS_PER_TURN_LIMIT = 100
GRAPH_PENDING_STEPS_SLOT = "_graph_pending_steps"
CANCELLED_ASSISTANT_REPLY = "已停止生成"
ExecutionFinalizeState = Literal["continued", "completed", "handoff"]


def _knowledge_scope_ids(
    scope: dict[str, Any],
    plural_key: str,
    singular_key: str,
) -> list[str]:
    values = scope.get(plural_key)
    if not isinstance(values, list):
        singular = scope.get(singular_key)
        values = [singular] if singular else []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _find_handoff_node_id_in_skill(
    skill: Skill, active_step_id: str | None = None
) -> str | None:
    """查找 SOP 中从当前节点可达的 handoff 节点。

    使用 GraphRules.find_handoff_node_id 做基于 edges 的 BFS,
    优先返回从 active_step_id 可达的 handoff 节点,而非数组顺序的第一个。
    """
    content = skill.content_json or {}
    return GraphRules.find_handoff_node_id(content, active_step_id)


def _agent_identity_prompt(agent: AgentProfile) -> str:
    return AgentIdentityPrompt.render(
        agent,
        single_line=_single_line_text,
        metadata_formatter=_metadata_prompt_text,
    )


def _metadata_prompt_text(value: object) -> str:
    if isinstance(value, str):
        return _single_line_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        items = [_single_line_text(item) for item in value]
        return "、".join(item for item in items if item)
    return ""


def _single_line_text(value: object) -> str:
    return AgentIdentityPrompt.single_line(value)


class AgentLoopPreconditionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class TurnStreamSink:
    """流式轮次的实时投递器：执行期阶段状态 + 生成期增量文本。

    改造前 ``handle_turn_stream`` 要先等 ``handle_turn`` 整轮跑完才能拿到
    ``response.reply`` 再切块下发，纯问答轮次因此要等「整轮耗时」才有第一个字。
    本类让执行期与生成期都能持续写入 ``AgentEvent`` relay 行并唤醒订阅该会话的
    SSE，前端看到的节奏变成「执行结束 + TTFT」。

    只在流式路径挂载：非流式 ``handle_turn`` 不构造它，行为与改造前一致。
    """

    def __init__(self, owner: AgentLoop) -> None:
        self._owner = owner
        self._tenant_id = ""
        self._session_id = ""
        self._buffer: list[str] = []
        self._streamed_parts: list[str] = []
        self._last_flush_at = 0.0
        self._started_at: float | None = None
        self.streamed_chars = 0

    @property
    def active(self) -> bool:
        return bool(self._tenant_id and self._session_id)

    @property
    def streamed(self) -> bool:
        return self.streamed_chars > 0

    def bind(self, tenant_id: str, session_id: str) -> None:
        self._tenant_id = str(tenant_id or "")
        self._session_id = str(session_id or "")
        self._buffer = []
        self._streamed_parts = []
        self._last_flush_at = 0.0
        self._started_at = None
        self.streamed_chars = 0

    def status(self, phase: str, text: str, **extra: object) -> None:
        if not self.active:
            return
        self._record("stream_status", {"phase": phase, "text": text, **extra})

    def delta(self, chunk: str) -> None:
        if not self.active or not chunk:
            return
        now = time.monotonic()
        if self._started_at is None:
            self._started_at = now
        self._buffer.append(chunk)
        self._streamed_parts.append(chunk)
        self.streamed_chars += len(chunk)
        pending = sum(len(item) for item in self._buffer)
        if (
            now - self._last_flush_at < self._flush_interval(now)
            and pending < STREAM_DELTA_FLUSH_MAX_CHARS
        ):
            return
        self.flush()

    def _flush_interval(self, now: float) -> float:
        """落库最小间隔：短回复固定 60ms，长回复按上限次数自动摊开。"""

        if self._started_at is None:
            return STREAM_DELTA_FLUSH_INTERVAL_SECONDS
        elapsed = max(0.0, now - self._started_at)
        return max(
            STREAM_DELTA_FLUSH_INTERVAL_SECONDS,
            elapsed / STREAM_DELTA_MAX_FLUSHES,
        )

    def flush(self) -> None:
        if not self.active or not self._buffer:
            return
        content = "".join(self._buffer)
        self._buffer = []
        self._last_flush_at = time.monotonic()
        self._record("stream_delta", {"content": content})

    def reset(self) -> None:
        """丢弃已下发的增量（生成中途失败降级时用，避免残留半截文本）。

        清空 ``streamed_chars`` 让调用方知道「流式内容已作废」，可以安全地
        按老路径重新分片下发。
        """
        if not self.active:
            return
        if not self.streamed_chars and not self._buffer:
            return
        self._buffer = []
        self._streamed_parts = []
        self.streamed_chars = 0
        self._last_flush_at = 0.0
        self._started_at = None
        self._record("stream_replace", {"content": ""})

    def replace(self, content: str) -> None:
        """用给定正文原地替换已下发内容（最终正文不可续写时用）。"""
        if not self.active:
            return
        self._buffer = []
        self._streamed_parts = [content] if content else []
        self._last_flush_at = time.monotonic()
        self.streamed_chars = len(content)
        self._record("stream_replace", {"content": content})

    def reconcile(self, content: str) -> None:
        """收尾对账：下发内容与最终正文不一致时用最终正文原地替换。

        预览通道（决策 JSON 的 ``finish.reply_fragment``）只负责「正文早出现」，
        正确性由这里兜底——引用编号重排、``finish`` 被拒后重来、降级话术、最终
        裁剪都会落到这条路径。两者一致时不产生任何事件。
        """

        if not self.active or not self.streamed_chars:
            return
        if "".join(self._streamed_parts) == content:
            return
        self.replace(content)

    def _record(self, event_type: str, payload: dict[str, object]) -> None:
        self._owner.events.record(
            self._tenant_id, self._session_id, event_type, dict(payload)
        )
        self._owner.db.commit()
        # relay 行已提交：唤醒监听该会话的 SSE 流（Redis 不可用时为 no-op，
        # SSE 走兜底轮询，正确性不受影响）。
        publish_relay_wake(self._session_id)
