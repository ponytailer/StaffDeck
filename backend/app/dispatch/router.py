"""路由决策：读用户这句话，选出一位成员，或者判断选不出来。

设计要点（见 docs/agent-dispatcher-plan.md §4.3）：
- **无成员 / 单成员直接短路**，零 LLM 调用（MVP 最常见的形态是单成员，省 1~2s）。
- 多成员才调一次小模型，走 ``llm_operation("dispatcher.route")``。
- 模型输出的任何非法值都在 ``_normalize`` 里被兜成 fallback，**绝不 500**。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlmodel import Session

from app import paths
from app.db.models import AgentProfile, DispatcherMember
from app.dispatch.schema import (
    DispatchConfig,
    DispatchRouteDecision,
)
from app.dispatch.service import (
    DEFAULT_CLARIFY_TEXT,
    build_route_context,
    default_clarify_text,
    last_routed_member_id,
)
from app.llm.stage_protocol import stage_payload
from app.observability.spans import llm_operation

if TYPE_CHECKING:  # pragma: no cover
    from app.llm.model_config_resolver import ResolvedModelConfig

PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "dispatcher_route_prompt.md"

OUTPUT_CONTRACT: dict[str, Any] = {
    "action": "route | clarify | reply",
    "member_agent_id": "string?",
    "confidence": "number",
    "intent": "string?",
    "query": "string?",
    "reason": "string?",
    "reply": "string?",
}


def _read_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


class DispatcherRouter:
    """把「这句话给谁」这个判断封装起来，便于单测打桩。"""

    def decide(
        self,
        *,
        db: Session,
        dispatcher: AgentProfile,
        members: list[tuple[DispatcherMember, AgentProfile]],
        message: str,
        config: DispatchConfig,
        parent_session: Any | None = None,
        model_config: "ResolvedModelConfig | None" = None,
    ) -> DispatchRouteDecision:
        text = (message or "").strip()

        # ① 无有效成员 → 直接给出兜底话术，零 LLM
        if not members:
            return DispatchRouteDecision(
                action="reply",
                reply=default_clarify_text(config),
                reason="没有可用的成员",
                fast_path=True,
            )

        # ② 单成员 → 直连，零 LLM。
        # 正常配置下不会有这个状态（成员下限是 MIN_DISPATCH_MEMBERS=2，API 层强校验），
        # 但成员被删除/归档后库里可能只剩 1 个，这时候直连仍然比报错好。
        if len(members) == 1:
            member = members[0][1]
            return DispatchRouteDecision(
                action="route",
                member_agent_id=member.id,
                confidence=1.0,
                intent=None,
                query=text,
                reason="只剩一位有效同事，直接转交",
                fast_path=True,
            )

        # ③ 兜底策略是「转给默认成员」且配了默认成员 → 零 LLM，但只在模型不可用时才这么做，
        #    否则还是让模型判断；这里留给 LLM 失败后的 fallback 使用，不在正常路径短路。

        route_context = build_route_context(parent_session) if parent_session is not None else ""
        payload = stage_payload(
            phase="Dispatcher / Route",
            user_message=text,
            conversation_context=None,
            memory_context=None,
            instructions=_read_prompt(),
            stage_data={
                "members": [
                    {
                        "agent_id": member.id,
                        "name": member.name,
                        "description": member.description or "",
                        "routing_rule": row.routing_rule or "",
                        "example_queries": list(row.example_queries_json or []),
                        "sort_order": row.sort_order,
                    }
                    for row, member in members
                ],
                "route_context": route_context,
                "last_routed_member_id": (
                    last_routed_member_id(parent_session) if parent_session is not None else None
                ),
                "fallback_hint": (
                    "无法判断时输出 action=clarify，并给出一句能让用户选的问题"
                ),
            },
            output_contract=OUTPUT_CONTRACT,
        )

        raw: Any = None
        if model_config is not None:
            from app.llm.client import LLMClient, LLMError

            try:
                with llm_operation("dispatcher.route"):
                    raw = LLMClient(model_config).generate_json(_read_prompt(), payload)
            except LLMError:
                return self._fallback(
                    config, members, text, reason="路由模型调用失败"
                )
            except Exception:  # 网络/解析等任何异常都不许冒到用户面前
                return self._fallback(
                    config, members, text, reason="路由模型异常"
                )

        decision = self._normalize(raw, members, config, text)
        return decision

    # ── 归一化：把所有非法输出都兜成合理结果 ──────────────────

    def _normalize(
        self,
        raw: Any,
        members: list[tuple[DispatcherMember, AgentProfile]],
        config: DispatchConfig,
        message: str,
    ) -> DispatchRouteDecision:
        if not isinstance(raw, dict):
            return self._fallback(config, members, message, reason="路由输出无法解析")

        action = str(raw.get("action") or "").strip().lower()
        valid_ids = {member.id for _row, member in members}
        member_agent_id = str(raw.get("member_agent_id") or "").strip()
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        if action == "reply":
            reply = str(raw.get("reply") or "").strip()
            return DispatchRouteDecision(
                action="reply",
                reply=reply or default_clarify_text(config),
                reason=str(raw.get("reason") or ""),
                confidence=confidence,
            )

        if action == "route":
            # 成员不在名单 / 置信度不足 / 成员是 dispatcher（脏数据）→ 一律降级
            if member_agent_id not in valid_ids:
                return self._fallback(
                    config, members, message, reason="选中的成员不在候选名单"
                )
            if confidence < config.min_confidence:
                return self._fallback(
                    config, members, message, reason="置信度不足"
                )
            return DispatchRouteDecision(
                action="route",
                member_agent_id=member_agent_id,
                confidence=confidence,
                intent=_clip(raw.get("intent"), 40),
                query=str(raw.get("query") or "").strip() or message,
                reason=_clip(raw.get("reason"), 200),
            )

        # action=clarify 或任何没见过的值
        reply = str(raw.get("reply") or "").strip()
        return self._fallback(config, members, message, reason="模型判断为需要澄清", reply=reply)

    def _fallback(
        self,
        config: DispatchConfig,
        members: list[tuple[DispatcherMember, AgentProfile]],
        message: str,
        *,
        reason: str = "",
        reply: str = "",
    ) -> DispatchRouteDecision:
        """按配置的兜底策略收尾。

        - ``reply`` → 固定话术
        - ``default_member`` → 转给配好的默认成员（没配就退化成 clarify）
        - ``clarify`` → 反问 + 可选候选
        """
        mode = config.fallback_mode

        if mode == "reply":
            return DispatchRouteDecision(
                action="reply",
                reply=(config.fallback_reply or default_clarify_text(config)),
                reason=reason,
            )

        if mode == "default_member":
            default_id = str(config.default_member_agent_id or "").strip()
            target = next(
                (member for _row, member in members if member.id == default_id), None
            )
            if target is not None:
                return DispatchRouteDecision(
                    action="route",
                    member_agent_id=target.id,
                    confidence=0.0,
                    intent=None,
                    query=message,
                    reason=f"{reason}；按配置转给默认同事",
                )
            # 配了模式但默认成员已失效 → 退化成 clarify，不要静默丢弃用户问题

        text = config.fallback_reply or DEFAULT_CLARIFY_TEXT
        if config.offer_candidates:
            lines = [
                f"- {member.name}" + (f"：{member.description}" if member.description else "")
                for _row, member in members
            ]
            text = f"{text}\n\n可以找这些同事：\n" + "\n".join(lines)
        return DispatchRouteDecision(
            action="clarify",
            reply=text,
            reason=reason,
        )


def _clip(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


def decision_payload(decision: DispatchRouteDecision) -> dict[str, Any]:
    """落 ``dispatch_decision`` 事件用。会脱敏——不放 prompt、不放成员私有规则。"""
    return json.loads(decision.model_dump_json())