"""Planner 快速路径与追问短路（零 LLM 合成 answer_only 计划）。

自 planner.py 拆出（行数控制）；planner.py 保持 re-export。判定原则与
守卫说明见各函数 docstring。
"""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from app.db.models import ChatSession, Skill
from app.session.session_schema import (
    PlannedTaskFrame,
    TeamPlannerContext,
    TurnPlan,
)


# Planner 快速路径的转人工类关键词：命中即回落 LLM 规划器。
# 宁可慢一轮 LLM，也不把「转人工」误按成知识问答——漏判只损失 12s，
# 错判是真实事故。英文小写匹配。
FAST_PATH_HANDOFF_KEYWORDS = (
    "转人工",
    "人工客服",
    "人工服务",
    "真人客服",
    "转接人工",
    "human agent",
    "human support",
    "real person",
)


def _fast_path_plan(
    session: ChatSession,
    message: str,
    available_skills: list[Skill],
    task_frame_state: list[dict[str, Any]] | None,
    interaction_mode: str,
    team_context: TeamPlannerContext | None,
) -> TurnPlan | None:
    """结构性守卫全部命中时，跳过 planner LLM 直接合成 answer_only 计划。

    守卫的设计原则：只在「LLM 没有任何路由决策空间」时直通——当前 agent
    没有配置任何 SOP、也没有任何进行中/待处理的任务语义。此时 LLM 的理性
    输出几乎必然是 answer_only + conversation 帧，12-15s 的规划调用是纯
    固定开销。任何守卫不满足即返回 None，回落完整 LLM 规划（不猜语义）。
    """
    settings = get_settings()
    if not settings.planner_fast_path_enabled:
        return
    if (
        interaction_mode != "normal" or team_context is not None \
        # 有 SOP 可选时必须走 LLM：消息可能想触发某个 SOP 的 trigger_intents。
        or available_skills \
        # 有活跃 SOP / 待处理任务 / 待补槽 / 恢复中的帧时，决策空间非空，走 LLM。
        or str(session.active_skill_id or "").strip() \
        or str(session.active_step_id or "").strip() \
        or session.pending_tasks_json \
        or task_frame_state or session.awaiting_input_json
    ):
        return
    text = " ".join(str(message or "").split()).strip()
    if not text or text.startswith("/"):
        return
    lowered = text.lower()
    if any(keyword in lowered for keyword in FAST_PATH_HANDOFF_KEYWORDS):
        return
    return TurnPlan(
        decision="answer_only",
        confidence=1.0,
        user_intent=text,
        reason="planner fast path: no SOP context, direct conversation",
        task_frames=[
            PlannedTaskFrame(
                kind="conversation",
                decision="answer_only",
                user_intent=text,
                source_message=message,
            )
        ],
    )


def _followup_shortcircuit_plan(
    session: ChatSession,
    message: str,
    available_skills: list[Skill],
    conversation_context: dict[str, object] | None,
    task_frame_state: list[dict[str, Any]] | None,
    interaction_mode: str,
    team_context: TeamPlannerContext | None,
) -> TurnPlan | None:
    """会话续跑短路（qa-optimization I6 增量）。

    fast path 只覆盖「没有任何 SOP」的员工；有 SOP 时短追问（S5：上一轮知识
    问答完成后「那住客呢」）仍会白走一次 planner LLM（实测 ~5s）。当结构性
    上下文同样排除路由空间、有对话历史（非首条消息）、消息很短、且不命中任何
    SOP 的 trigger_intents 关键词时，合成 answer_only 计划直接续跑会话帧。
    任何守卫不满足即返回 None，回落完整 LLM 规划（不猜语义）。
    """

    settings = get_settings()
    if not settings.planner_followup_shortcircuit_enabled:
        return
    if (
        interaction_mode != "normal"
        or team_context is not None
        or str(session.active_skill_id or "").strip()
        or str(session.active_step_id or "").strip()
        or session.pending_tasks_json
        or task_frame_state
        or session.awaiting_input_json
    ):
        return
    if not conversation_context:
        # 首条消息没有对话历史，语义判断交给完整 planner。
        return
    text = " ".join(str(message or "").split()).strip()
    if not text or text.startswith("/") or len(text) > 20:
        return
    lowered = text.lower()
    if any(keyword in lowered for keyword in FAST_PATH_HANDOFF_KEYWORDS):
        return
    for skill in available_skills:
        content = skill.content_json if isinstance(skill.content_json, dict) else {}
        for trigger in content.get("trigger_intents") or []:
            trigger_text = str(trigger or "").strip().lower()
            if trigger_text and trigger_text in lowered:
                # 消息可能想触发某个 SOP，路由决策空间非空，走 LLM。
                return
    return TurnPlan(
        decision="answer_only",
        confidence=0.0,
        user_intent=text,
        reason="planner followup short-circuit: short follow-up, no SOP trigger",
        task_frames=[
            PlannedTaskFrame(
                kind="conversation",
                decision="answer_only",
                user_intent=text,
                source_message=message,
            )
        ],
    )
