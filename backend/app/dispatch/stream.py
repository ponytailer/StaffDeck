"""调度委派的主流程：一轮对话 = 一次路由 + 一次子 turn + 答案原样回流。

**为什么不用 /chat/stream 内部 HTTP 调用**：那条路会重新做鉴权、配额、scope 校验，
而且它的 SSE 属于另一个 HTTP 响应，没法并进当前这条流。这里直接内部调
``AgentLoop.handle_turn_stream``，把子会话的增量**镜像成父会话的 AgentEvent 行**——
前端 SSE 只读父会话的事件（chat.py 的 ``_events_after_cursor``），所以**前端零改动**。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from sqlmodel import Session, select

from app.db.database import engine
from app.db.models import AgentEvent, AgentProfile, ChatSession, Message, utc_now
from app.dispatch.router import DispatcherRouter, decision_payload
from app.dispatch.schema import DispatchRouteDecision
from app.dispatch.service import (
    ACTIVE_DISPATCH_KEY,
    DISPATCH_CANCEL_KEY,
    DispatchValidationError,
    append_dispatch_log,
    build_child_context,
    child_session_turn_id,
    default_clarify_text,
    dispatch_config,
    ensure_child_session,
    last_routed_member_id,
    load_active_members_with_agents,
    resolve_dispatch_member_model,
    resolve_dispatcher_model,
)
from app.session.session_schema import ChatTurnRequest

logger = logging.getLogger(__name__)

# 只镜像这些事件到父会话。**不镜像** session_created / user_message_received /
# complete —— 这三个由父会话自己发，否则前端会出现两个会话 id 或两条用户消息。
MIRROR_EVENTS = {
    "stream_status",
    "stream_delta",
    "stream_replace",
    "tool_result",
    "knowledge_result",
    "artifact",
}

# 增量镜像的合并窗口。
#
# 子 turn 的每个 token 都会经 event_sink 同步镜像成**父会话的一条 relay 行 + 一次
# commit**（`_persist_relay_only_event`）。远程库实测 commit ≈ 56ms，一轮 40+ 个
# 分片就是 2.4s，而且这些 commit 是**串行卡在子 turn 的生成循环里**的（event_sink
# 是同步回调，不合并就等于每个 token 付一次往返）。实测同一问题：
#   独立 agent 13.44s / dispatcher 17.30s —— 差距 3.9s 里约 2.4s 是这个开销。
# 合并窗口把 commit 次数从「每 token」降到「每 80ms」，与前端的 stream_delta
# 100ms 合批对齐，观感不变（前端本来也是 100ms 才更新一次）。
MIRROR_DELTA_COALESCE_SECONDS = 0.08
# 单次合并的内容上限：长回复要保证持续吐字，不能攒太大一次性发。
MIRROR_DELTA_COALESCE_MAX_CHARS = 240

# 子 turn 里需要把 sessionId 改写成父会话 id 的字段
SESSION_ID_KEYS = ("sessionId", "session_id")


def _rewrite_session(payload: dict[str, Any], parent_session_id: str) -> dict[str, Any]:
    for key in SESSION_ID_KEYS:
        if key in payload:
            payload[key] = parent_session_id
    return payload


def _child_citations(child_message: Message | None) -> list[dict[str, Any]]:
    if child_message is None:
        return []
    metadata = child_message.metadata_json or {}
    citations = metadata.get("knowledge_citations")
    return list(citations) if isinstance(citations, list) else []


def _child_artifacts(child_message: Message | None) -> list[dict[str, Any]]:
    if child_message is None:
        return []
    metadata = child_message.metadata_json or {}
    artifacts = metadata.get("harness_artifacts") or metadata.get("artifacts")
    return list(artifacts) if isinstance(artifacts, list) else []


def _latest_assistant_message(db: Session, session_id: str) -> Message | None:
    return db.exec(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())
    ).first()


def _cancel_requested(db: Session, parent_session_id: str) -> bool:
    """路由窗口（1~2s）内用户点了取消。

    取消端点会往父会话的 context_state_json 里置 ``dispatch_cancel_requested``；
    起了子 turn 之前必须查一次，否则会出现「取消按钮转了但没停」。

    **必须 refresh**：取消是另一个请求/线程改的库，本 session 的 identity map
    里还是 run() 开头清空标记后的那份旧值，直接 ``db.get`` 永远读到 False。
    """
    session = db.get(ChatSession, parent_session_id)
    if session is None:
        return False
    db.refresh(session)
    return bool((session.context_state_json or {}).get(DISPATCH_CANCEL_KEY))


def _clear_cancel_flag(db: Session, parent: ChatSession) -> None:
    metadata = dict(parent.context_state_json or {})
    if DISPATCH_CANCEL_KEY in metadata:
        metadata.pop(DISPATCH_CANCEL_KEY, None)
        parent.context_state_json = metadata
        db.add(parent)


def _record_active_dispatch(
    db: Session, parent: ChatSession, child_session_id: str, child_turn_id: str
) -> None:
    metadata = dict(parent.context_state_json or {})
    metadata[ACTIVE_DISPATCH_KEY] = {
        "child_session_id": child_session_id,
        "child_turn_id": child_turn_id,
    }
    parent.context_state_json = metadata
    parent.updated_at = utc_now()
    db.add(parent)
    db.commit()


def _clear_active_dispatch(db: Session, parent_session_id: str) -> None:
    session = db.get(ChatSession, parent_session_id)
    if session is None:
        return
    metadata = dict(session.context_state_json or {})
    if metadata.pop(ACTIVE_DISPATCH_KEY, None) is not None:
        session.context_state_json = metadata
        session.updated_at = utc_now()
        db.add(session)
        db.commit()


class DispatchTurnRunner:
    """跑一轮调度委派。构造时注入外部依赖，便于单测替换。"""

    def __init__(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], None],
        router: DispatcherRouter | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._emit = emit
        self._router = router or DispatcherRouter()
        self._session_factory = session_factory or (lambda: Session(engine))
        # 本轮子 turn 已实时镜像出去的流式文本事件数。调用方（``_run_dispatch_stream``）
        # 靠它决定「还要不要整段回放答案」——镜像过就别回放，否则答案会发两遍
        # （实测 total_chars 恰好等于 reply 长度 ×2，前端还会把后段重复追加）。
        self._mirrored_text_count = 0
        # 增量合批缓冲（每轮在 _stream_child_turn 里重置）
        self._pending_delta: list[str] = []
        self._last_delta_emit_at = 0.0

    # ── 入口 ──────────────────────────────────────────────

    @property
    def mirrored_text_count(self) -> int:
        """本轮已实时镜像出去的流式文本事件数（0 表示没送过增量）。

        调用方据此决定要不要整段回放答案，避免答案发两遍。
        """
        return self._mirrored_text_count

    def run(
        self,
        db: Session,
        request: ChatTurnRequest,
        parent: ChatSession,
        dispatcher: AgentProfile,
        *,
        parent_turn_id: str = "",
    ) -> dict[str, Any]:
        """执行一轮，返回最终 complete 事件要用的 payload。

        ``parent_turn_id`` 是父会话这一轮用户消息的 id，**必须透传**：父会话的
        assistant 消息要带着它落库，否则 ``message_read`` 推不出 turn_id，前端的
        ``computeMergedMessages`` 就无法把这轮的服务端消息与实时流式消息认成同一轮
        → 同一个答案渲染成两个气泡（真实踩过）。
        """
        # 上一轮遗留的取消标记必须在本轮开头清掉，否则「取消过一次」会把
        # 这个会话之后每一轮都直接取消掉。
        _clear_cancel_flag(db, parent)
        db.commit()

        config = dispatch_config(dispatcher)
        members = load_active_members_with_agents(db, request.tenant_id, dispatcher.id)

        # ① 路由决策
        try:
            dispatcher_model = resolve_dispatcher_model(
                db, request.tenant_id, dispatcher, request.user_id
            )
            decision = self._router.decide(
                db=db,
                dispatcher=dispatcher,
                members=members,
                message=request.message,
                config=config,
                parent_session=parent,
                model_config=dispatcher_model,
            )
        except DispatchValidationError as exc:
            # 没有可用模型等配置类错误：给用户可读提示，不冒技术栈
            logger.warning("dispatcher route precondition failed: %s", exc)
            return self._finish_with_text(
                db,
                parent,
                request,
                "调度入口暂时不可用：还没配置模型。请联系管理员在配置里选一个模型。",
                turn_id=parent_turn_id,
                dispatch_payload={"action": "error", "reason": str(exc)},
            )
        except Exception:
            logger.exception("dispatcher route failed")
            return self._finish_with_text(
                db,
                parent,
                request,
                "这句话没能判断出该找哪位同事，换个说法再试一次？",
                turn_id=parent_turn_id,
                dispatch_payload={"action": "error"},
            )

        # 决策事件里带上成员名：前端要在子 turn 开始前就显示「转给某人」，
        # 否则那 20~30s 里界面只有「正在思考」，用户不知道是谁在处理。
        decision_member = next(
            (m for _row, m in members if m.id == decision.member_agent_id), None
        )
        self._emit(
            "dispatch_decision",
            {
                "sessionId": parent.id,
                **decision_payload(decision),
                "member_name": decision_member.name if decision_member else None,
                "member_count": len(members),
            },
        )

        # ② fallback 分支：clarify / reply
        if decision.action in {"clarify", "reply"}:
            text = (decision.reply or "").strip() or default_clarify_text(config)
            return self._finish_with_text(
                db,
                parent,
                request,
                text,
                turn_id=parent_turn_id,
                dispatch_payload={
                    "action": decision.action,
                    "reason": decision.reason,
                    "member_count": len(members),
                },
            )

        # ③ route 分支
        member = next(
            (m for _row, m in members if m.id == decision.member_agent_id), None
        )
        if member is None:
            # 成员在路由后失效（并发删除/归档）→ 走兜底，不报错
            text = default_clarify_text(config)
            return self._finish_with_text(
                db,
                parent,
                request,
                text,
                turn_id=parent_turn_id,
                dispatch_payload={
                    "action": "clarify",
                    "reason": "目标成员已失效",
                    "member_count": len(members),
                },
            )

        return self._run_member_turn(
            db,
            request,
            parent,
            dispatcher=dispatcher,
            member=member,
            decision=decision,
            dispatcher_model=dispatcher_model,
            turn_id=parent_turn_id,
        )

    # ── 真正委派 ──────────────────────────────────────────

    def _run_member_turn(
        self,
        db: Session,
        request: ChatTurnRequest,
        parent: ChatSession,
        *,
        dispatcher: AgentProfile,
        member: AgentProfile,
        decision: DispatchRouteDecision,
        dispatcher_model: Any,
        turn_id: str = "",
    ) -> dict[str, Any]:
        previous_member_id = last_routed_member_id(parent)
        same_member = previous_member_id == member.id

        child = ensure_child_session(db, parent, member)
        db.commit()

        member_model = resolve_dispatch_member_model(
            db, request.tenant_id, member, dispatcher_model, request.user_id
        )
        child_turn_id = child_session_turn_id(parent.id, member.id, request.client_turn_id)
        query = (decision.query or "").strip() or request.message

        child_request = ChatTurnRequest(
            tenant_id=request.tenant_id,
            session_id=child.id,
            agent_id=member.id,
            # 显式带 model_config_id：跳过 _get_default_model 的免费回落，
            # 同时让 chat.py 的免费配额校验永不触发（方案 §3）
            model_config_id=member_model.id,
            user_id=request.user_id,
            client_turn_id=child_turn_id,
            message=query,
            context_injection=build_child_context(
                parent, member_name=member.name, same_member=same_member
            ),
            channel="web",
            interaction_mode="normal",
            message_visibility="visible",
            web_search=request.web_search,
        )

        # 让前端知道「已经转给谁了」——状态条靠这个 phase
        self._emit(
            "stream_status",
            {
                "sessionId": parent.id,
                "phase": "dispatched",
                "text": f"已转交给 {member.name}",
                "member_agent_id": member.id,
                "member_name": member.name,
                "client_turn_id": request.client_turn_id,
            },
        )

        _record_active_dispatch(db, parent, child.id, child_turn_id)

        # 路由窗口内被取消：不再起子 turn（成本省下来，体验也对）。
        # **必须放在落「转接通知」之前**：否则用户点了取消，会话里会留下一条
        # 「已转交给 X」的孤立通知，后面永远不会有答案跟上来（真实踩过）。
        if _cancel_requested(db, parent.id):
            _clear_active_dispatch(db, parent.id)
            self._emit(
                "stream_cancelled",
                {"sessionId": parent.id, "client_turn_id": request.client_turn_id},
            )
            return {"cancelled": True}

        # 落一条「路由转接」通知：让对话里先出现由**路由**（而不是成员）说的
        # 「已转交给 X」，成员答案作为它后面的一条回复。之前只有答案气泡上挂
        # 一个「由 X 回答」小 chip，用户读不出「谁决定转的、转给了谁」这层结构。
        #
        # 三个刻意的约定，改动前必须一起看（真实踩过）：
        # 1. **content 必须有值**：靠它当气泡正文（前端按 metadata.dispatch_notice
        #    渲染成路由身份），空串会被 MessageList 的「空助手消息」守卫直接跳过。
        # 2. **必须带 turn_id / client_turn_id**：要和答案共享同一轮，才能排到答案
        #    前面（答案锚定在用户消息的轮次起点上，没有 turn 归属的消息会排到后面）。
        #    所以 **turn_id 为空时干脆不落这条通知**——没有轮次归属的通知会被排到
        #    答案后面，「路由先说、成员后答」的顺序就反了。
        # 3. **不能算「这一轮有答案」**：前端 hasAssistantMessageForTurn 必须把
        #    dispatch_notice 排除，否则子 turn 还没开始就被判定成已完成 →
        #    stream.loading 提前回 false、轮询停止、答案永远接不上。
        if turn_id:
            notice_metadata: dict[str, Any] = {
                "dispatch_notice": {
                    "member_agent_id": member.id,
                    "member_name": member.name,
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                },
                "turn_id": turn_id,
                "user_message_id": turn_id,
            }
            if request.client_turn_id:
                notice_metadata["client_turn_id"] = request.client_turn_id
            db.add(
                Message(
                    tenant_id=request.tenant_id,
                    session_id=parent.id,
                    role="assistant",
                    content=f"已转交给 {member.name}",
                    metadata_json=notice_metadata,
                )
            )
            parent.updated_at = utc_now()
            db.add(parent)
            db.commit()
            self._emit(
                "dispatch_notice",
                {
                    "sessionId": parent.id,
                    "member_agent_id": member.id,
                    "member_name": member.name,
                    "intent": decision.intent,
                    "confidence": decision.confidence,
                    "turn_id": turn_id,
                    "user_message_id": turn_id,
                    "client_turn_id": request.client_turn_id,
                },
            )

        reply = ""
        failure: str | None = None
        try:
            reply = self._stream_child_turn(
                child_request,
                parent_session_id=parent.id,
                parent_turn_id=turn_id,
                parent_client_turn_id=request.client_turn_id,
            )
        except Exception as exc:  # 子 turn 失败不许冒到用户面前
            logger.exception("dispatch child turn failed")
            failure = exc.__class__.__name__
        finally:
            _clear_active_dispatch(db, parent.id)

        if failure is not None or not reply.strip():
            text = (
                f"转交给 {member.name} 处理时出错了，可以稍后重试，或者换个问法。"
                if failure is not None
                else f"{member.name} 这次没有给出回复，换个问法再试试？"
            )
            self._emit(
                "dispatch_failed",
                {
                    "sessionId": parent.id,
                    "member_agent_id": member.id,
                    "member_name": member.name,
                    "error_type": failure,
                },
            )
            return self._finish_with_text(
                db,
                parent,
                request,
                text,
                turn_id=turn_id,
                dispatch_payload={
                    "action": "error",
                    "member_agent_id": member.id,
                    "member_name": member.name,
                },
            )

        # 父会话落答案快照（仅展示，不参与任何 prompt）
        child_message = _latest_assistant_message(db, child.id)
        citations = _child_citations(child_message)
        artifacts = _child_artifacts(child_message)
        metadata: dict[str, Any] = {
            "dispatch": {
                "member_agent_id": member.id,
                "member_name": member.name,
                "child_session_id": child.id,
                "intent": decision.intent,
                "confidence": decision.confidence,
                "query": query,
            }
        }
        if citations:
            metadata["knowledge_citations"] = citations
        if artifacts:
            metadata["harness_artifacts"] = artifacts
        # turn_id 必须落进 metadata：父会话这轮的 assistant 消息没有对应的
        # assistant_message_created 事件，``message_read`` 只能靠 metadata 回退推
        # turn_id。缺了它前端会把这条服务端消息和实时流式消息当成两轮，答案重复显示。
        # client_turn_id 同理：前端刷新后靠它把本地 `turn_xxx` 与服务端轮次认成一轮，
        # 缺了它 `hasAssistantMessageForTurn` 会一直判定「这轮没有答案」→ loadTraces
        # 把已结束的轮次当僵尸复活 → stream.loading 永久为 true → 后续消息全被塞进
        # 本地队列、界面卡在「排队中」（真实踩过）。
        if turn_id:
            metadata["turn_id"] = turn_id
            metadata["user_message_id"] = turn_id
        if request.client_turn_id:
            metadata["client_turn_id"] = request.client_turn_id
        answer_message = Message(
            tenant_id=request.tenant_id,
            session_id=parent.id,
            role="assistant",
            content=reply,
            metadata_json=metadata,
        )
        db.add(answer_message)
        db.flush()
        # **必须补一条 ``assistant_message_created``**：``_build_turn_traces`` 只认
        # 这个事件来给 trace 收尾（置 ``completed_at`` + 收掉 running 行）。dispatch
        # 分支以前从不写它，于是那一轮的 ``completed_at`` 恒为 null，最后被
        # ``fallback_time = events[-1].created_at`` 强行补上——那个时间是**整个会话
        # 最后一条事件**的时间。结果就是「成员实际跑了 30s，界面显示总耗时 3min」
        # （真实踩过，实测某轮 51min vs 真实 6.3s）。
        db.add(
            AgentEvent(
                tenant_id=request.tenant_id,
                session_id=parent.id,
                event_type="assistant_message_created",
                payload_json={
                    "message_id": answer_message.id,
                    "assistant_message_id": answer_message.id,
                    "user_message_id": turn_id or answer_message.id,
                    "turn_id": turn_id or answer_message.id,
                    "client_turn_id": request.client_turn_id,
                    "reply": reply,
                },
            )
        )
        append_dispatch_log(
            db,
            parent,
            {
                "turn_id": child_message.id if child_message is not None else "",
                "member_agent_id": member.id,
                "member_name": member.name,
                "intent": decision.intent or "",
                "user_query": request.message,
                "confidence": decision.confidence,
            },
        )
        parent.updated_at = utc_now()
        db.add(parent)
        db.commit()

        return {
            "reply": reply,
            "session_id": parent.id,
            "dispatch": {
                "member_agent_id": member.id,
                "member_name": member.name,
                "child_session_id": child.id,
                "intent": decision.intent,
                "confidence": decision.confidence,
            },
        }

    def _stream_child_turn(
        self,
        child_request: ChatTurnRequest,
        *,
        parent_session_id: str,
        parent_turn_id: str = "",
        parent_client_turn_id: str | None = None,
    ) -> str:
        """在**独立 db 会话**里跑子 turn，把增量镜像到父会话的 relay 行。

        独立会话是必需的：子 turn 有自己的事务与锁，混用父会话的 Session
        会把两边的事务边界搅在一起。

        **镜像走 ``event_sink``，不是生成器 yield**（真实踩过）：
        ``TurnStreamSink`` 的真实流式增量是**直接写子会话 AgentEvent 表**的
        （``sink.delta → _record → EventLog.record``），根本不经过
        ``handle_turn_stream`` 的生成器。只转发生成器的话，父会话一条真增量都
        拿不到，只剩最后那段「整段答案切块回放」——用户看到的就是「转交后
        界面卡三分钟、答案一次性蹦出来」。
        ``EventLog.record`` 会回调 ``event_sink``，所以挂上它就是**覆盖全部
        已持久化事件**（真增量 + 分片回放兜底），且两路不会重复发。
        """
        from app.core.engine.facade import AgentLoop

        reply = ""
        child_session_id = child_request.session_id

        def _mirror(event_type: str, payload: dict[str, Any]) -> None:
            if event_type not in MIRROR_EVENTS:
                return
            forwarded = _rewrite_session(dict(payload), parent_session_id)
            # **轮次归属必须改写成父会话这一轮**：子 turn 的 turn_id 是子会话
            # 用户消息 id，前端父会话认不出它，delta 会被丢进「无主轮次」而
            # 不落到正在流式的气泡上。client_turn_id 一并透传，刷新后 PWA
            # 的别名并查集才能把两边的 turn 认成一轮。
            if parent_turn_id:
                forwarded["turn_id"] = parent_turn_id
                forwarded["user_message_id"] = parent_turn_id
            # client_turn_id 用**父轮**的（不是子轮那个带成员后缀的）：
            # 前端别名并查集靠它把本地 `turn_xxx` 与服务端轮次认成一轮。
            if parent_client_turn_id:
                forwarded["client_turn_id"] = parent_client_turn_id
            forwarded["dispatch_child_session_id"] = child_session_id

            # ── 增量：先攒着，达到时间/长度阈值再合并成一条发出去 ──
            # 见 MIRROR_DELTA_COALESCE_SECONDS 的注释（每条都是一次 56ms 的 commit）。
            if event_type in {"stream_delta", "token"}:
                self._mirrored_text_count += 1
                piece = str(forwarded.get("content") or "")
                if piece:
                    self._pending_delta.append(piece)
                if not self._pending_delta:
                    return
                pending_chars = sum(len(item) for item in self._pending_delta)
                if (
                    pending_chars < MIRROR_DELTA_COALESCE_MAX_CHARS
                    and time.monotonic() - self._last_delta_emit_at
                    < MIRROR_DELTA_COALESCE_SECONDS
                ):
                    return
                _flush_delta()
                return

            if event_type == "stream_replace":
                # replace 是「用这份正文整体替换已下发内容」，攒着的旧增量天然作废，
                # 直接丢弃，别先发一遍再被覆盖（白发一次 commit）。
                self._pending_delta = []
                self._mirrored_text_count += 1
                self._emit(event_type, forwarded)
                return

            # 其它非增量事件（状态 / 工具结果 / artifact）：先把攒的正文吐出去，
            # 保证父会话里「正文」与「状态」的相对顺序不变。
            _flush_delta()
            self._emit(event_type, forwarded)

        def _flush_delta() -> None:
            """把攒着的增量拼成一条 ``stream_delta`` 发出去（并清空缓冲）。"""
            if not self._pending_delta:
                return
            content = "".join(self._pending_delta)
            self._pending_delta = []
            self._last_delta_emit_at = time.monotonic()
            if not content:
                return
            payload_out: dict[str, Any] = {
                "sessionId": parent_session_id,
                "content": content,
                "dispatch_child_session_id": child_session_id,
            }
            if parent_turn_id:
                payload_out["turn_id"] = parent_turn_id
                payload_out["user_message_id"] = parent_turn_id
            if parent_client_turn_id:
                payload_out["client_turn_id"] = parent_client_turn_id
            self._emit("stream_delta", payload_out)

        child_db = self._session_factory()
        self._pending_delta = []
        self._last_delta_emit_at = 0.0
        try:
            for item in AgentLoop(child_db, event_sink=_mirror).handle_turn_stream(child_request):
                event_name = str(item.get("event") or "")
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                if event_name in {"complete", "done"}:
                    reply = str(data.get("reply") or "")
                    continue
                # 生成器只用来取最终 reply：镜像已由 event_sink 全覆盖，
                # 这里再转发一遍就会出现两份重复增量。
        finally:
            # 兜底：循环结束后把最后一段攒着的增量吐出去，否则答案尾部会丢。
            try:
                _flush_delta()
            except Exception:
                logger.exception("dispatch mirror flush failed")
            child_db.close()
        return reply

    # ── 直接给出文本（clarify / reply / error） ─────────────

    def _finish_with_text(
        self,
        db: Session,
        parent: ChatSession,
        request: ChatTurnRequest,
        text: str,
        *,
        turn_id: str = "",
        dispatch_payload: dict[str, Any],
    ) -> dict[str, Any]:
        # 同 _run_member_turn：metadata 里必须带 turn_id，否则前端按轮去重会失效
        # （这条消息没有 assistant_message_created 事件可反查）。
        metadata: dict[str, Any] = {"dispatch": dispatch_payload}
        if turn_id:
            metadata["turn_id"] = turn_id
            metadata["user_message_id"] = turn_id
        # 与 _run_member_turn 同口径：前端刷新后要靠 client_turn_id 把本地
        # `turn_xxx` 与服务端轮次认成同一轮（buildTurnAliasMap 读它）。
        # clarfy / reply / error 分支同样可能被刷新后拉到，缺了就会触发
        # loadTraces 的「僵尸复活」→ stream.loading 永久 true → 消息卡「排队中」。
        if request.client_turn_id:
            metadata["client_turn_id"] = request.client_turn_id
        answer_message = Message(
            tenant_id=request.tenant_id,
            session_id=parent.id,
            role="assistant",
            content=text,
            metadata_json=metadata,
        )
        db.add(answer_message)
        db.flush()
        # 与 _run_member_turn 同口径：兜底回复也要有 assistant_message_created，
        # 否则这一轮的 trace 永远不会 completed_at → 被 events[-1] 强行补时间 →
        # 总耗时虚高（clarify/reply 分支同样会踩）。
        if turn_id:
            db.add(
                AgentEvent(
                    tenant_id=request.tenant_id,
                    session_id=parent.id,
                    event_type="assistant_message_created",
                    payload_json={
                        "message_id": answer_message.id,
                        "assistant_message_id": answer_message.id,
                        "user_message_id": turn_id,
                        "turn_id": turn_id,
                        "client_turn_id": request.client_turn_id,
                        "reply": text,
                    },
                )
            )
        parent.updated_at = utc_now()
        db.add(parent)
        db.commit()
        return {
            "reply": text,
            "session_id": parent.id,
            "dispatch": dispatch_payload,
        }