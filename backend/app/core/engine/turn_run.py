"""Turn 主循环：run 编排、会话租约续期与收尾（批次 4.3 自 turn.py 类体搬出，mixin 化，零行为改动）。"""

from __future__ import annotations

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.aigw_cache import get_json, set_json
from app.config import get_settings
from app.core.base.cancellation import is_chat_turn_cancelled
from app.core.capability.discovery import project_capability_manifest
from app.core.capability.manifest import CapabilityManifestBuilder
from app.core.base.conversation_projection import ConversationProjection
from app.core.engine.step_loop import (
    MODEL_UNAVAILABLE_FAILURE_CODE,
    HarnessExecutionCancelled,
    HarnessExecutionFenced,
    HarnessTaskAgent,
    _trace_capability_result,
)
from app.core.engine.attachments import (
    materialize_task_attachments,
    validated_task_image_payloads,
)
from app.core.engine.invoke import HarnessCapabilityInvoker
from app.core.knowledge_flow.prefetch import is_smalltalk
from app.core.capability.deliverables import list_published_deliverables
from app.core.engine.session import (
    HarnessSessionLeaseLost,
    HarnessSessionLeaseStore,
    HarnessSessionLeaseToken,
)
from app.core.engine.session import (
    acquire_harness_session,
    release_harness_session,
)
from app.core.engine.session import HarnessTurnStore
from app.core.plan.slash import (
    SlashCommandError,
    SlashCommandSelection,
    build_slash_turn_plan,
    force_capability_for_requirement,
    parse_slash_command,
    resolve_capability,
    slash_command_message,
)
from app.core.sop.slots.form import build_slot_form
from app.core.sop.slots.hydration import SlotHydrationPolicy
from app.core.engine.frames import (
    TaskFrameClaimConflict,
    TaskFrameStore,
    planned_frame_from_record,
)
from app.core.base.request_compiler import (
    TaskExecutionResult,
    TaskRequestCompiler,
)
from app.core.plan.planner import TurnPlanner, turn_plan_router_decision
from app.core.base.ui_flags import knowledge_citations_hidden
from app.db.models import (
    ChatSession,
    HarnessRunRecord,
    HarnessTaskFrameRecord,
    HarnessTurnRecord,
    Message,
    Skill,
    Team,
    utc_now,
)
from app.knowledge.citations import (
    citation_identity,
    compact_knowledge_citation_labels,
    renumber_citations,
    strip_citation_markers,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.memory.service import memory_read
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    StepAgentResult,
    TurnPlan,
)
from app.skills.nesting import discoverable_sops, expand_visible_sops

from app.core.engine.turn_support import (

    _aggregate_artifacts,
    _append_session_handoff_artifact,
    _build_knowledge_laya_router,
    _build_laya_router,
    _combine_results,
    _defer_failed_step_after_completed_checkpoint,
    _dependency_order,
    _enforce_required_slots,
    _globalize_citations,
    _inject_handoff_context,
    _is_recoverable_action_protocol_failure,
    _load_edge_condition_specs,
    _merge_discovered_artifacts,
    _prior_result,
    _response_task_payload,
    _restore_session_state,
    _session_state,
    _single_task_reply,
    _skill_step_timeout_seconds,
    _sibling_task_intents,
    _source_user_message,
    _step_result,
    _with_recoverable_first_session,
    get_or_create_harness_session,
)

from app.core.engine.turn_context import (
    INTENT_CACHE_TTL_SECONDS,
    KNOWLEDGE_ANSWER_OPERATION,
    _KNOWLEDGE_WORKFLOW_BLOCKING_KINDS,
    _TRACE_STREAM_STATUS,
    _apply_forced_sop_snapshot,
    _capability_summary_text,
    _direct_reply_answer,
    _direct_reply_answer_stream,
    _intent_cache_key,
    _is_knowledge_workflow_frame,
    _session_title_from_plan,
    _stream_status_for_trace,
    _turn_planner_message,
    _turn_skill_projection,
    _turn_slash_selection,
)


logger = logging.getLogger(__name__)


class _TurnRunMixin:
    def run(self, request: ChatTurnRequest) -> ChatTurnResponse:
        session_request = _with_recoverable_first_session(request)
        if session_request.session_id:
            self._session_lock_id = session_request.session_id
            self._session_lock = acquire_harness_session(
                session_request.session_id
            )
        session = self._get_or_create_session(session_request)
        self.session = session
        if self._session_lock is None:
            self._session_lock_id = session.id
            self._session_lock = acquire_harness_session(session.id)
        self.session_lease = self.session_leases.acquire(session)
        turn_claim = self.turn_store.claim(session, request)
        self.turn_record = turn_claim.record
        if turn_claim.replay is not None:
            return turn_claim.replay
        self.owner._mark_session_running(session)
        user_message = self.owner._append_message(
            request.tenant_id,
            session.id,
            "user",
            request.message,
            metadata=self.owner._user_message_metadata(request),
        )
        self.user_message_id = user_message.id
        self.current_source_turn_id = user_message.id
        self.turn_store.bind_user_message(
            self.turn_record,
            user_message.id,
        )
        bind_turn = getattr(self.events, "bind_turn", None)
        if callable(bind_turn):
            bind_turn(user_message.id, request.client_turn_id)
        self.events.record(
            request.tenant_id,
            session.id,
            "user_message_received",
            {
                "message_id": user_message.id,
                "turn_id": user_message.id,
                "client_turn_id": request.client_turn_id,
                "message": request.message,
                "channel": request.channel,
                "user_id": request.user_id,
                "execution_engine": "harness_v2",
                **(
                    {"message_visibility": request.message_visibility}
                    if request.message_visibility != "visible"
                    else {}
                ),
            },
        )

        self.slash_command = _turn_slash_selection(request)
        execution_message = (
            slash_command_message(self.slash_command)
            if self.slash_command
            else request.message
        )
        if request.context_injection:
            execution_message = f"{request.context_injection.rstrip()}\n\n{execution_message}"
        execution_request = request.model_copy(
            update={"message": execution_message, "context_injection": None}
        )

        model_config = self.owner._get_request_model(request, session.agent_id)
        if model_config is None:
            raise RuntimeError("没有默认模型配置。")
        # 意图识别（Turn Planner）是纯分类/任务拆分任务，允许使用更轻量的
        # 专用模型。用户在模型配置中勾选「用于意图识别」后，该阶段优先使用
        # 该模型；未配置或解析失败时回退到本轮请求的模型，行为与历史一致。
        planner_model_config = self.owner._get_default_model(
            request.tenant_id,
            session.agent_id,
            "intent_recognition",
            user_id=request.user_id,
        ) or model_config
        source_skills = self.owner._list_published_skills(
            request.tenant_id, session.agent_id
        )
        source_skills = _apply_forced_sop_snapshot(
            source_skills,
            request.forced_sop_id,
            request.forced_sop_snapshot,
        )
        # Team TL conversations use a dedicated ChatSession, so the leader can
        # safely execute their own SOPs without mutating a personal chat. Keep
        # the same published/discoverable SOP boundary in every interaction
        # mode; team orchestration remains an additional conversation concern,
        # not a reason to hide the leader's executable workflow.
        skills, routing_skills = _turn_skill_projection(
            source_skills,
            interaction_mode=request.interaction_mode,
        )
        self.owner._drop_unavailable_skill_state(
            request.tenant_id, session, skills
        )
        memory_context = [
            memory_read(row)
            for row in self.owner.memory.context_memories(
                request.tenant_id,
                request.user_id,
                agent_id=session.agent_id,
            )
        ]
        if memory_context:
            self.events.record(
                request.tenant_id,
                session.id,
                "memory_recalled",
                {
                    "memories": memory_context,
                    "execution_engine": "harness_v2",
                },
            )
        self.db.commit()
        self.db.refresh(session)
        self._raise_if_cancelled(request, session)
        conversation_context = self.owner._conversation_context(
            session, model_config=model_config
        )

        self._renew_session_lease()
        planner_state = self.store.planner_state(session)
        team_context = request.team_context
        team: Team | None = None
        if request.interaction_mode == "team_tl" and session.team_id:
            team = self.db.get(Team, session.team_id)
            if team is not None and team_context is None:
                from app.teams.wakeup import build_team_planner_context

                team_context = build_team_planner_context(self.db, team)
        if self.slash_command:
            if self.slash_command.kind in {"skill", "tool"}:
                direct_manifest = self.manifests.build(
                    request.tenant_id,
                    session.agent_id,
                    None,
                    None,
                )
                resolve_capability(self.slash_command, direct_manifest)
            plan = build_slash_turn_plan(
                self.slash_command,
                execution_request.message,
                session,
                routing_skills,
                planner_state,
            )
        else:
            # 意图识别缓存：指纹含全部 planner 输入，命中即省一次模型调用；
            # Redis 不可用/指纹不可算 → 直接走原调用，行为与改造前一致。
            intent_cache_key = _intent_cache_key(
                session,
                routing_skills,
                request,
                conversation_context,
                memory_context,
                planner_state,
                team_context,
            )
            plan: TurnPlan | None = None
            if intent_cache_key:
                cached_plan = get_json(intent_cache_key)
                if isinstance(cached_plan, dict):
                    try:
                        plan = TurnPlan.model_validate(cached_plan)
                    except Exception:  # noqa: BLE001 - 缓存损坏按未命中处理
                        plan = None
            if plan is None:
                # planner 是在 user_message_received 事件转发到 stream worker
                # 之前执行的，其 LLM span 若不显式携带 turn_id，诊断脚本就无法
                # 把 planner 慢调用归因到具体 turn（观测缺口）。ContextVar
                # 属性合并机制允许这里直接注入。
                from app.observability.spans import llm_span_attributes

                with llm_span_attributes(
                    turn_id=self.user_message_id,
                    user_message_id=self.user_message_id,
                ):
                    plan = self.planner.plan(
                        _turn_planner_message(request),
                        session,
                        routing_skills,
                        planner_model_config,
                        deepcopy(conversation_context),
                        memory_context,
                        planner_state,
                        interaction_mode=request.interaction_mode,
                        team_context=team_context,
                    )
                if intent_cache_key:
                    set_json(
                        intent_cache_key,
                        plan.model_dump(mode="json"),
                        ttl_seconds=INTENT_CACHE_TTL_SECONDS,
                    )
        self._renew_session_lease()
        self._raise_if_cancelled(request, session)
        slot_hydration = SlotHydrationPolicy.hydrate_plan(
            session,
            plan,
            skills,
            memory_context,
        )
        if slot_hydration:
            self.events.record(
                request.tenant_id,
                session.id,
                "slots_hydrated",
                {
                    **slot_hydration,
                    "source": "memory",
                    "turn_id": user_message.id,
                    "execution_engine": "harness_v2",
                },
            )
        router_decision = turn_plan_router_decision(plan)
        # 会话标题（2026-09-15）：不再用 LLM 起标题。planner 已产出
        # user_intent（一句话意图），SOP 帧直接用 SOP 名——首轮规划完成即
        # 落标题，比原「后台线程调 LLM」（实测 13-17s）快且零成本。
        # 兜底链：本处 → chat._summarize_session_title_once（已持久化意图）
        # → agent_loop._finalize_turn（首条用户消息）。手动改过的不覆盖。
        if not (session.title or "").strip():
            plan_title = _session_title_from_plan(plan, skills)
            if plan_title:
                session.title = plan_title
                self.db.add(session)
        self.events.record(
            request.tenant_id,
            session.id,
            "turn_plan_created",
            {
                **plan.model_dump(mode="json"),
                "turn_id": user_message.id,
                "execution_engine": "harness_v2",
            },
        )
        # Keep the old public trace event while making the new planner explicit.
        self.events.record(
            request.tenant_id,
            session.id,
            "router_decision_created",
            {
                **router_decision.model_dump(mode="json"),
                "turn_id": user_message.id,
                "execution_engine": "harness_v2",
            },
        )
        team_publish_result = None
        remote_frames = [
            frame for frame in plan.task_frames if frame.execution_target == "team_member"
        ]
        if remote_frames:
            if request.interaction_mode != "team_tl" or team is None:
                raise RuntimeError("团队成员 TaskFrame 缺少可信团队上下文。")
            from app.teams.wakeup import publish_team_planner_frames

            team_publish_result = publish_team_planner_frames(
                self.db,
                team=team,
                session=session,
                source_turn_id=user_message.id,
                created_by_user_id=request.user_id,
                frames=remote_frames,
            )
            if team_publish_result is None:
                raise RuntimeError("团队成员 TaskFrame 未能持久化，已停止本轮虚假分发。")
            plan = plan.model_copy(
                update={
                    "task_frames": [
                        frame
                        for frame in plan.task_frames
                        if frame.execution_target != "team_member"
                    ]
                }
            )
        if plan.decision == "complete_task":
            active_task_frame_id = self.store.active_task_frame_id(session)
            self.store.complete_active_frame(
                session,
                reason=plan.reason or "用户结束了当前任务。",
                task_id=plan.selected_task_id,
            )
            if (
                session.active_skill_id
                and (
                    not plan.selected_task_id
                    or plan.selected_task_id == active_task_frame_id
                )
            ):
                self.owner.runtime.complete_current_skill(session)
            self.events.record(
                request.tenant_id,
                session.id,
                "task_frame_completed",
                {
                    "selected_task_id": plan.selected_task_id,
                    "reason": plan.reason,
                    "execution_engine": "harness_v2",
                },
            )

        pre_turn_state = _session_state(session)
        records = self.store.persist_plan(session, user_message.id, plan)
        known_record_ids = {row.task_id for row in records}
        records.extend(
            self.store.ready_dependency_frames(
                session,
                exclude_task_ids=known_record_ids,
            )
        )
        records = _dependency_order(records)
        known_record_ids = {row.task_id for row in records}
        # execution_mode 仅存在于本轮 plan（不持久化）：deferred 恢复帧查不到
        # 映射时回退 standard，安全降级到完整 Harness 路径。
        light_execution_mode_by_frame_id: dict[str, str] = {
            frame.task_id: frame.execution_mode
            for frame in plan.task_frames
            if frame.kind == "conversation"
            and frame.execution_mode in {"direct_reply", "capability_intro"}
            and frame.task_id
        }
        self.db.commit()
        self.db.refresh(session)

        remaining_turn_actions = max(
            0,
            int(
                self.owner._get_agent_loop_max_actions(
                    request.tenant_id, session.agent_id
                )
            ),
        )
        execution_payloads: list[dict[str, object]] = []
        execution_results: list[TaskExecutionResult] = []
        last_step_result = StepAgentResult()
        last_skill: Skill | None = None
        if records:
            # 规划已结束、进入执行：给等待期一个明确的阶段提示（流式轮次才生效）。
            self._emit_stream_status("preparing", "正在执行任务")

        for record_index, row in enumerate(records):
            self._renew_session_lease()
            if remaining_turn_actions <= 0:
                deferred_rows = records[record_index:]
                self.store.defer_for_action_budget(deferred_rows)
                self.events.record(
                    request.tenant_id,
                    session.id,
                    "turn_action_budget_exhausted",
                    {
                        "deferred_task_frame_ids": [
                            item.task_id for item in deferred_rows
                        ],
                        "execution_engine": "harness_v2",
                    },
                )
                self.db.commit()
                break
            self._raise_if_cancelled(request, session)
            if not self.store.dependencies_satisfied(row, records):
                self.store.defer_for_dependencies(row)
                waiting = TaskExecutionResult(
                    task_frame_id=row.task_id,
                    status="blocked",
                    reply_fragment="前置任务完成后将自动继续该任务。",
                    task_summary="TaskFrame 正在等待前置任务完成。",
                    error={"code": "DEPENDENCY_WAITING"},
                )
                self.events.record(
                    request.tenant_id,
                    session.id,
                    "task_frame_dependency_waiting",
                    {
                        "task_frame_id": row.task_id,
                        "depends_on_task_ids": list(row.depends_on_json or []),
                        "execution_engine": "harness_v2",
                    },
                )
                execution_results.append(waiting)
                execution_payloads.append(
                    _response_task_payload(
                        row,
                        waiting,
                        None,
                        StepAgentResult(reply=waiting.reply_fragment),
                    )
                )
                continue

            frame = planned_frame_from_record(row)
            active_skill = self._activate_frame(session, row, skills)
            if frame.kind == "sop" and active_skill is None:
                failed = TaskExecutionResult(
                    task_frame_id=row.task_id,
                    status="failed",
                    reply_fragment="对应的 SOP 当前不可用。",
                    task_summary="SOP 在执行前已下线或解绑。",
                    error={"code": "SOP_NOT_AVAILABLE"},
                )
                self.store.finish_frame(
                    row,
                    status="failed",
                    step_id=row.step_id,
                    slots=dict(row.slots_json or {}),
                    result=failed.model_dump(mode="json"),
                )
                execution_results.append(failed)
                execution_payloads.append(
                    _response_task_payload(
                        row,
                        failed,
                        None,
                        StepAgentResult(reply=failed.reply_fragment),
                    )
                )
                continue

            # conversation 帧分级：direct_reply / capability_intro 走轻量直出，
            # 跳过完整 Harness 决策循环（43.6s 问题的主路径）。任何异常降级
            # 到标准路径。
            light_mode = light_execution_mode_by_frame_id.get(row.task_id)
            if frame.kind == "conversation" and light_mode:
                direct_result = self._run_direct_reply_frame(
                    request,
                    execution_request,
                    session,
                    row,
                    frame,
                    model_config,
                    memory_context,
                    conversation_context,
                    execution_mode=light_mode,
                )
                if direct_result is not None:
                    remaining_turn_actions = max(
                        0,
                        remaining_turn_actions - max(1, direct_result.action_count),
                    )
                    execution_results.append(direct_result)
                    execution_payloads.append(
                        _response_task_payload(
                            row,
                            direct_result,
                            None,
                            StepAgentResult(reply=direct_result.reply_fragment),
                        )
                    )
                    continue
                # 直通失败（降级返回 None）→ 按标准 conversation 帧继续。

            last_skill = active_skill or last_skill
            combined, step_result = self._run_frame(
                execution_request,
                session,
                row,
                frame,
                active_skill,
                model_config,
                memory_context,
                [
                    *self.store.dependency_results(row),
                    *self.store.referenced_session_results(row),
                ],
                remaining_turn_actions,
                lightweight_model_config=(
                    planner_model_config
                    if planner_model_config is not model_config
                    else None
                ),
                conversation_context=conversation_context,
            )
            remaining_turn_actions = max(
                0,
                remaining_turn_actions - max(1, combined.action_count),
            )
            last_step_result = step_result
            execution_results.append(combined)
            execution_payloads.append(
                _response_task_payload(
                    row,
                    combined,
                    active_skill,
                    step_result,
                )
            )
            if row.status == "completed":
                released = self.store.ready_dependency_frames(
                    session,
                    exclude_task_ids=known_record_ids,
                )
                if released:
                    records.extend(released)
                    known_record_ids.update(
                        item.task_id for item in released
                    )
                    self.events.record(
                        request.tenant_id,
                        session.id,
                        "task_frame_dependencies_released",
                        {
                            "completed_task_frame_id": row.task_id,
                            "released_task_frame_ids": [
                                item.task_id for item in released
                            ],
                            "execution_engine": "harness_v2",
                        },
                    )

        self._restore_visible_active_frame(
            session,
            records,
            pre_turn_state,
        )
        self.store.project_session(session)
        self.db.commit()
        self.db.refresh(session)
        self._raise_if_cancelled(request, session)
        citations = _globalize_citations(execution_results)
        for payload, result in zip(
            execution_payloads,
            execution_results,
            strict=False,
        ):
            payload["knowledge_citations"] = list(result.citations)
        _inject_handoff_context(self.db, session, execution_payloads, execution_results, request)

        # ``last_skill`` is the execution-expanded parent graph. Prefer it so a
        # nested SOP's response rules remain available after the child graph
        # reaches a terminal node. Falling back to the stored row is only
        # needed for turns that did not execute a TaskFrame.
        response_skill = last_skill or self.owner._get_active_skill(
            request.tenant_id, session.active_skill_id, session.agent_id
        )
        self._renew_session_lease()
        reply = _single_task_reply(execution_results)
        if team_publish_result is not None and not execution_results:
            task_count = len(team_publish_result.task_ids)
            reply = (
                f"已完成 {task_count} 个团队任务的拆分与派发。"
            )
        if reply is None:
            reply = self._synthesize_reply(
                request,
                execution_request,
                session,
                response_skill,
                router_decision,
                last_step_result,
                model_config,
                memory_context,
                conversation_context,
                execution_payloads,
            )
        self._renew_session_lease()
        reply, citations = compact_knowledge_citation_labels(reply, citations)
        # 与 agent_loop 同一口径：租户关掉「展示知识来源」时正文里不留 [n] 编号
        if knowledge_citations_hidden(self.db, request.tenant_id):
            reply = strip_citation_markers(reply)
        artifacts = _aggregate_artifacts(execution_results)
        assistant_metadata: dict[str, Any] = {
            "execution_engine": "harness_v2",
            "task_frame_ids": [row.task_id for row in records],
        }
        if team_publish_result is not None:
            assistant_metadata["team_run_id"] = team_publish_result.run_id
            assistant_metadata["team_task_ids"] = list(team_publish_result.task_ids)
            assistant_metadata["team_progress"] = {
                "phase": "collecting",
                "completed_tasks": 0,
                "total_tasks": len(team_publish_result.task_ids),
                "status_text": "正在等待成员回复",
            }
        if request.client_turn_id:
            assistant_metadata["client_turn_id"] = request.client_turn_id
        if request.message_visibility != "visible":
            assistant_metadata["message_visibility"] = request.message_visibility
        if citations:
            assistant_metadata["knowledge_citations"] = citations
        if artifacts:
            assistant_metadata["harness_artifacts"] = artifacts
        # A2UI（MVP）：缺槽询问附带的表单描述。取最后一条带表单的结果——同一轮
        # 里后续帧若已把信息问齐，前面那条表单就不该再展示。文案（reply）照旧
        # 保留，非 UI 渠道只吃文本。
        ui_form = next(
            (
                item.ui_form
                for item in reversed(execution_results)
                if getattr(item, "ui_form", None)
            ),
            None,
        )
        if ui_form:
            assistant_metadata["a2ui"] = ui_form
        if self.slash_command:
            assistant_metadata["slash_command"] = self.slash_command.model_dump(
                mode="json"
            )
        # Cancellation and normal projection compete for this durable receipt.
        # Only the winner may append a terminal assistant message.
        self._raise_if_cancelled(request, session)
        self.turn_store.begin_completion(self.turn_record)
        reply = self.owner._finalize_turn(
            session,
            request.tenant_id,
            reply,
            last_step_result,
            request.message,
            user_message_id=user_message.id,
            assistant_metadata_override=assistant_metadata,
        )
        self.db.commit()
        self.db.refresh(session)
        if request.message_visibility == "visible":
            self.owner._enqueue_memory_capture(
                request,
                session,
                last_step_result,
                None,
                model_config,
            )
        response = ChatTurnResponse(
            reply=reply,
            session_id=session.id,
            router_decision=router_decision,
            step_result=last_step_result,
            tool_result=None,
            session_state=public_session(session),
        )
        self.turn_store.complete(self.turn_record, response)
        return response

    def close(self) -> None:
        session_id = str(self._session_lock_id or "")
        try:
            self.session_leases.release(self.session_lease)
        finally:
            self.session_lease = None
            release_harness_session(session_id, self._session_lock)
            self._session_lock = None
            self._session_lock_id = None

