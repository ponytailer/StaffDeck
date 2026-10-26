"""Turn 准备阶段辅助：SOP 投影、意图缓存键、能力清单摘要、轻量直答、流式状态、斜杠指令。

批次 4.2b 从 ``engine/turn.py`` 类定义之前搬出的模块级纯函数，零行为改动。
``turn.py`` 统一从这里导入并保持 re-export，测试的
``from app.core.engine.turn import X`` 路径不变。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from app.core.base.conversation_projection import ConversationProjection
from app.core.plan.slash import (
    SlashCommandError,
    SlashCommandSelection,
    parse_slash_command,
)
from app.db.models import ChatSession, Skill
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.session.session_schema import ChatTurnRequest, TurnPlan
from app.skills.nesting import discoverable_sops, expand_visible_sops

logger = logging.getLogger(__name__)

INTENT_CACHE_TTL_SECONDS = 600

# 知识库作业流唯一一次作答的 operation 名。单独命名是为了让 output_policy
# 能精确地「只关这条路径的思考链」，不波及通用对话作答。
KNOWLEDGE_ANSWER_OPERATION = "response.knowledge_answer"


def _turn_skill_projection(
    source_skills: list[Skill],
    *,
    interaction_mode: str,
) -> tuple[list[Skill], list[Skill]]:
    """Project executable and routable SOPs for every supported turn mode.

    Team TL conversations already own a dedicated session, so hiding the
    leader's SOPs here would only disable valid work; it is not required for
    state isolation.
    """

    _ = interaction_mode
    skills = expand_visible_sops(source_skills)
    return skills, discoverable_sops(skills)


def _session_title_from_plan(plan: TurnPlan, skills: list[Skill]) -> str:
    """从 planner 输出推导会话标题：SOP 帧用 SOP 名，否则用一句话意图。

    交给 ConversationProjection.fallback_session_title 做空白/句尾标点
    清洗并截断到 28 字符。推导不出（无意图且无 SOP 帧）返回空串。
    """
    for frame in plan.task_frames:
        if frame.kind == "sop" and frame.target_skill_id:
            skill = next(
                (item for item in skills if item.skill_id == frame.target_skill_id),
                None,
            )
            if skill and (skill.name or "").strip():
                title = ConversationProjection.fallback_session_title(skill.name)
                if title:
                    return title
    if (plan.user_intent or "").strip():
        return ConversationProjection.fallback_session_title(plan.user_intent)
    return ""


def _turn_planner_message(
    request: ChatTurnRequest,
) -> str:
    """Keep server-only execution context out of Planner task requirements."""

    return request.message


def _intent_cache_key(
    session: ChatSession,
    routing_skills: list[Skill],
    request: ChatTurnRequest,
    conversation_context: dict[str, Any] | None,
    memory_context: list[dict[str, Any]],
    planner_state: list[dict[str, Any]],
    team_context: Any,
) -> str | None:
    """按 planner 的**全部输入**计算指纹；不可序列化返回 None（不缓存）。

    指纹必须覆盖 conversation_context/memory_context/planner_state 等易变
    输入——上下文一变指纹即变，天然不会把旧 plan 用到新轮次上。
    """
    try:
        material = {
            "agent_id": session.agent_id,
            "session_id": session.id,
            "message": _turn_planner_message(request),
            "interaction_mode": request.interaction_mode,
            "skills": [[s.skill_id, s.version] for s in routing_skills],
            "conversation_context": conversation_context,
            "memory_context": memory_context,
            "planner_state": planner_state,
            "team_context": (
                team_context.model_dump(mode="json") if team_context is not None else None
            ),
        }
        raw = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
        return f"intent:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
    except Exception:
        return None


def _capability_summary_text(
    db: Any,
    tenant_id: str,
    agent_id: str | None,
) -> str:
    """capability_intro 帧的能力清单摘要：从可见的 GeneralSkill / 知识库 /
    Tool 提取 name + description，拼成给直通模型的简洁清单。

    只做展示性摘要（每项截断到 ~120 字），不做授权判定——真正的能力授权
    仍以 CapabilityManifestBuilder 为准，直通回复只是「介绍」，不执行任何能力。
    任何异常由调用方捕获后降级到标准路径。
    """

    from app.core.capability.manifest import CapabilityManifestBuilder
    from app.core.capability.discovery import project_capability_manifest

    manifest = CapabilityManifestBuilder(db).build(
        tenant_id,
        agent_id,
        skill=None,
        step_id=None,
    )
    projected = project_capability_manifest(manifest, budget_chars=2_000)
    entries = projected.catalog or []
    lines: list[str] = []
    for entry in entries:
        description = str(entry.description or "").strip()
        if len(description) > 120:
            description = description[:117] + "..."
        name = str(entry.name or "").strip()
        kind_label = {
            "general_skill": "技能",
            "knowledge": "知识库检索",
            "tool": "工具",
            "file": "文件处理",
            "internal": "内部",
        }.get(entry.kind, entry.kind)
        lines.append(f"- [{kind_label}] {name}：{description}" if description else f"- [{kind_label}] {name}")
    if not lines:
        return ""
    return "\n".join(lines)


# 遇到这些 kind 说明不只知识库（有技能/工具要模型自己编排）→ 不走直答工作流。
# 与 app/core/knowledge_prefetch.py 的 _BLOCKING_KINDS 同口径。
_KNOWLEDGE_WORKFLOW_BLOCKING_KINDS = {"general_skill", "tool"}


def _is_knowledge_workflow_frame(
    frame: Any,
    manifest: Any,
    *,
    slash_command: Any,
    has_attachments: bool,
) -> bool:
    """判断 conversation 帧是否可走「仅知识库」确定性工作流。

    条件：conversation + answer_only + 无斜杠命令 + 无附件；可用能力里没有
    general_skill/tool（即只有知识库检索可用）；knowledge_search 在授权清单里。
    不满足一律返回 False，回退标准 Harness 循环。
    """

    if frame.kind != "conversation":
        return False
    if str(getattr(frame, "decision", "") or "") != "answer_only":
        return False
    if slash_command is not None or has_attachments:
        return False
    if manifest is None or "knowledge_search" not in manifest.allowed_names():
        return False
    for item in manifest.available:
        if (
            getattr(item, "available", False)
            and str(getattr(item, "kind", "") or "")
            in _KNOWLEDGE_WORKFLOW_BLOCKING_KINDS
        ):
            return False
    return True


def _direct_reply_answer(
    message: str,
    persona_prompt: str | None,
    memory_context: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    answer_model_config: Any,
    capability_summary: str | None = None,
) -> str:
    """direct_reply / capability_intro 帧的轻量直出回复：一次 chat 调用，不走
    Harness 决策循环。

    问答模型（若配置）优先；未配置时由调用方传入本轮请求模型。任何异常都
    抛给调用方降级到标准 Harness 路径，不劣于现状。
    """

    return "".join(
        _direct_reply_answer_stream(
            message,
            persona_prompt,
            memory_context,
            conversation_context,
            answer_model_config,
            capability_summary=capability_summary,
        )
    ).strip()


def _direct_reply_answer_stream(
    message: str,
    persona_prompt: str | None,
    memory_context: list[dict[str, Any]],
    conversation_context: dict[str, Any] | None,
    answer_model_config: Any,
    capability_summary: str | None = None,
) -> Iterator[str]:
    """``_direct_reply_answer`` 的流式版本：同一个请求体，改用
    ``generate_text_stream``，让纯问答轮次的首字延迟降到 TTFT。"""

    from app.core.converse import response as response_generator_module

    if capability_summary:
        execution_directive = (
            "用户消息是在询问你（数字员工）自身的能力/职责/用途。"
            "请结合下方提供的当前能力清单，以数字员工身份直接生成一份简短、"
            "自然、有条理的能力介绍，帮助用户了解你可以帮他做什么。"
            "只介绍能力清单中真实存在的能力，不要虚构；能力清单为空时，"
            "请坦率说明当前暂无可用能力并引导用户提出具体问题。"
        )
    else:
        execution_directive = (
            "用户消息是无需业务知识的社交性内容（问候/寒暄/道谢/告别）。"
            "请以数字员工身份直接生成简短自然的回复，1~3 句话即可，"
            "不要提问业务问题，不要展开任务。"
        )

    stage_data: dict[str, Any] = {
        "execution_directive": execution_directive,
    }
    if capability_summary:
        stage_data["capability_summary"] = capability_summary
    if persona_prompt:
        stage_data["employee_identity"] = persona_prompt.strip()
    if memory_context:
        stage_data["memory_context"] = [
            item for item in memory_context if isinstance(item, dict)
        ]
    payload = stage_payload(
        phase="Response Generator",
        user_message=message,
        conversation_context=(
            conversation_context if isinstance(conversation_context, dict) else {}
        ),
        memory_context=None,
        instructions=response_generator_module.PROMPT_PATH.read_text(encoding="utf-8"),
        stage_data=stage_data,
        output_contract=(
            "只输出最终用户可见的 Markdown 正文，不输出 JSON 外壳、分析过程或内部状态；"
            "除非用户明确请求代码，否则不要使用 Markdown 代码围栏。"
        ),
    )
    from app.llm import LLMClient
    from app.observability.spans import llm_operation

    with llm_operation("response.direct_reply"):
        yield from LLMClient(answer_model_config).generate_text_stream(
            unified_system_prompt(), payload
        )


# 执行期 trace 事件 → 前端可读的 stream_status。
# phase 一律取前端既有词表里的值（前端零改动即可渲染），text 用中文描述当前
# 阶段；前端把 status 行按 `decision_status_<phase>` 归并，因此同一 phase 的
# 连续状态会原地刷新成「当前在做什么」，而不是堆出一串历史行。
_TRACE_STREAM_STATUS: dict[str, tuple[str, str]] = {
    "knowledge_source_read": ("preparing", "读取知识来源"),
    "knowledge_payload_compacted": ("preparing", "整理知识证据"),
    "capability_search_completed": ("preparing", "匹配可用能力"),
    "capability_described": ("preparing", "加载能力说明"),
    "harness_action_model_retry": ("preparing", "模型输出异常，正在重试"),
    "harness_action_repair_requested": ("preparing", "正在修正模型输出格式"),
    "harness_completion_blocked": ("preparing", "正在补足必需能力后重试"),
    "harness_action_sequence_accepted": ("preparing", "按模型给出的动作序列执行"),
    "harness_structured_result_adapted": ("preparing", "整理结构化业务结果"),
    "sop_slot_extraction": ("preparing", "正在提取槽位信息"),
}


def _stream_status_for_trace(
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """把 harness 执行期的 trace 事件翻译成用户可见的等待提示。

    只有「用户此刻确实在等」的环节才翻译；finish 动作不产生新行，
    避免静默结束前多出一条无意义的滚动状态。
    """

    if event_type == "harness_action_created":
        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name:
            return None
        return {
            "phase": "preparing",
            "text": f"正在调用工具 {tool_name}",
            "tool_name": tool_name,
        }
    mapped = _TRACE_STREAM_STATUS.get(event_type)
    if mapped is None:
        return None
    phase, text = mapped
    extra: dict[str, Any] = {}
    query = str(payload.get("query") or "").strip()
    if query:
        extra["query"] = query
    return {"phase": phase, "text": text, **extra}


def _apply_forced_sop_snapshot(
    source_skills: list[Skill],
    forced_sop_id: str | None,
    snapshot: dict[str, Any] | None,
) -> list[Skill]:
    """Replace one currently accessible SOP with its immutable scheduled snapshot."""

    target = str(forced_sop_id or "").strip()
    if not target or not snapshot:
        return source_skills
    if str(snapshot.get("skill_id") or "").strip() != target:
        raise SlashCommandError(
            "FORCED_SOP_SNAPSHOT_INVALID",
            "定时任务保存的 SOP 快照与指定 SOP 不一致。",
        )
    content = snapshot.get("content_json")
    if not isinstance(content, dict):
        raise SlashCommandError(
            "FORCED_SOP_SNAPSHOT_INVALID",
            "定时任务保存的 SOP 快照内容无效。",
        )
    current = next((skill for skill in source_skills if skill.skill_id == target), None)
    if current is None:
        # Keep the normal capability-access error from resolve_sop. A historical
        # snapshot must never resurrect an SOP that is no longer bound/visible.
        return source_skills
    pinned = Skill(
        id=current.id,
        tenant_id=current.tenant_id,
        skill_id=current.skill_id,
        version=str(snapshot.get("version") or current.version),
        name=str(snapshot.get("name") or current.name),
        business_domain=(
            str(snapshot.get("business_domain"))
            if snapshot.get("business_domain") is not None
            else current.business_domain
        ),
        description=(
            str(snapshot.get("description"))
            if snapshot.get("description") is not None
            else current.description
        ),
        content_json=deepcopy(content),
        status="published",
        created_at=current.created_at,
        updated_at=current.updated_at,
    )
    if hasattr(current, "agent_branch_meta"):
        object.__setattr__(pinned, "agent_branch_meta", getattr(current, "agent_branch_meta"))
    return [pinned if skill.skill_id == target else skill for skill in source_skills]


def _turn_slash_selection(request: ChatTurnRequest) -> SlashCommandSelection | None:
    """Resolve user slash commands and server-pinned scheduled SOPs uniformly."""

    selection = parse_slash_command(request.message)
    forced_sop_id = str(request.forced_sop_id or "").strip()
    if forced_sop_id:
        if selection is not None:
            raise SlashCommandError(
                "FORCED_SOP_COMMAND_CONFLICT",
                "内部指定的 SOP 不能与用户斜杠指令同时使用。",
            )
        return SlashCommandSelection(
            kind="sop",
            target=forced_sop_id,
            prompt=request.message,
            raw=f"/sop {forced_sop_id}",
        )
    if selection and request.interaction_mode == "scheduled_task":
        raise SlashCommandError(
            "SLASH_COMMAND_MODE_CONFLICT",
            "定时任务执行不能从任务文本解析斜杠指令，请使用结构化 SOP 选择。",
        )
    return selection
