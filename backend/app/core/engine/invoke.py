from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.capabilities.local_general_skill import (
    package_from_row,
)
from app.core.capability.discovery import (
    CAPABILITY_SEARCH_MAX_RESULTS,
    catalog_entry,
    model_descriptor,
    search_capability_descriptors,
)
from app.core.capability.manifest import (
    CapabilityAuthorizationError,
    CapabilityManifestBuilder,
    general_skill_snapshot_digest,
    tool_snapshot_digest,
)
from app.core.engine.step_loop import HarnessExecutionCancelled
from app.core.capability.deliverables import (
    MAX_PUBLISHED_DELIVERABLES,
    find_published_deliverable,
    list_published_deliverables,
)
from app.core.engine.session import harness_task_workspace_path
from app.core.base.request_compiler import CapabilityDescriptor, CapabilityManifest
from app.core.capability.replay_policy import ToolReplayPolicy
from app.db.models import (
    ChatSession,
    GeneralSkill,
    HarnessInvocationRecord,
    KnowledgeChunk,
    KnowledgeConcept,
    ModelConfig,
    Skill,
    Tool,
    UIConfig,
    new_id,
    utc_now,
)
from app.harness import (
    HarnessArtifactAccessError,
    HarnessExecutor,
    HarnessToolCall,
    HarnessToolContext,
    build_file_tool_registry,
    open_harness_artifact,
    publish_changed_harness_artifacts,
    register_command_tools,
    register_skill_script_tools,
    snapshot_harness_workspace,
)
from app.harness.execution_context import SANDBOX_WORKSPACE
from app.harness.errors import HarnessExecutionError
from app.harness.sandbox import parse_network_policy
from app.knowledge.citations import knowledge_citations_from_results
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.search_payload import (
    KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS,
    compact_knowledge_search_payload,
    knowledge_payload_budget,
    serialized_chars,
)
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall

from app.core.engine.invoke_support import (
    _audit_arguments,
    _audit_result,
    _clip_for_read,
    _command_nonzero_hint,
    _command_nonzero_message,
    _command_timeout_message,
    _failure,
    _failure_was_not_sent,
    _intersect_knowledge_metadata,
    _is_user_facing_workspace_file,
    _materialize_general_skill_package,
    _model_visible_file_result,
    _replayed_result,
    _request_digest,
    _safe_artifact_label,
    _safe_artifact_name,
    _safe_general_skill_file_path,
    _sandbox_path,
    _skill_package_preview,
    _unique_non_empty,
    _workspace_root,
    _GENERAL_SKILL_PACKAGE_DIRECTORY,
    _INLINE_JSON_TOOL_RESULT_MAX_CHARS,
    _INTERNAL_TOOL_RESULT_DIRECTORY,
    _KNOWLEDGE_SOURCE_READ_DEFAULT_CHARS,
    _KNOWLEDGE_SOURCE_READ_MAX_CHARS,
    _KNOWLEDGE_SOURCE_READ_MAX_IDS,
    _KNOWLEDGE_SOURCE_READ_TOOL_NAME,
    _SANDBOX_JSON_FILE_KIND,
)
from app.core.engine.invoke_support import (  # noqa: F401  (re-export)
    _audit_arguments,
    _audit_result,
    _clip_for_read,
    _failure,
    _failure_was_not_sent,
    _intersect_knowledge_metadata,
    _materialize_general_skill_package,
    _replayed_result,
    _request_digest,
    _safe_artifact_label,
    _safe_artifact_name,
    _safe_general_skill_file_path,
    _sandbox_path,
    _skill_package_preview,
    _unique_non_empty,
    _workspace_root,
)
from app.core.engine.invoke_introspect import _CapabilityInfoMixin
from app.core.engine.invoke_tools import _ToolPackageMixin


_INLINE_JSON_TOOL_RESULT_MAX_CHARS = 2_000
_INTERNAL_TOOL_RESULT_DIRECTORY = ".harness/tool-results"
_GENERAL_SKILL_PACKAGE_DIRECTORY = ".harness/skill-packages"
_SANDBOX_JSON_FILE_KIND = "sandbox_json_file"
# 知识检索结果内联后正文按预算裁剪，需要逐字核对时用这个窄接口按 id 取原文
# （替代「让模型盲读整包 JSON」）。
_KNOWLEDGE_SOURCE_READ_TOOL_NAME = "read_knowledge_chunk"
_KNOWLEDGE_SOURCE_READ_MAX_IDS = 6
_KNOWLEDGE_SOURCE_READ_DEFAULT_CHARS = 4_000
_KNOWLEDGE_SOURCE_READ_MAX_CHARS = 12_000


class HarnessCapabilityInvoker(_ToolPackageMixin, _CapabilityInfoMixin):
    """Executes only capabilities frozen into one TaskFrame manifest."""

    def __init__(
        self,
        db: Any,
        *,
        tenant_id: str,
        session: ChatSession,
        task_frame_id: str,
        model_config: ModelConfig,
        manifest: CapabilityManifest,
        active_skill: Skill | None,
        active_step_id: str | None,
        agent_id: str | None,
        run_id: str | None = None,
        user_message: str | None = None,
        initially_activated_names: set[str] | None = None,
        is_cancelled: Any | None = None,
        ensure_execution_lease: Any | None = None,
        trace_sink: Callable[[str, dict[str, Any]], None] | None = None,
        step_deadline_monotonic: float | None = None,
        knowledge_laya_router: Any | None = None,
        # 本轮「互联网检索」开关。与 manifest 构建时的 web_search 同源同值：
        # 授权校验重建清单时必须带着它，否则开启态会被误判为撤权。
        web_search_enabled: bool = False,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.session = session
        self.task_frame_id = task_frame_id
        self.model_config = model_config
        self.manifest = manifest
        self.active_skill = active_skill
        self.active_skill_id = (
            active_skill.skill_id if active_skill is not None else None
        )
        self.active_step_id = active_step_id
        self.agent_id = agent_id
        # 本轮用户原话：仅作知识检索缓存位点的稳定维度。模型填的 query 每轮改写
        # 措辞都可能不同，用它做缓存 key 会导致「同一个问题问两次」也不命中。
        self.user_message = str(user_message or "").strip()
        self.is_cancelled = is_cancelled
        self.ensure_execution_lease = ensure_execution_lease
        self.trace_sink = trace_sink
        self.step_deadline_monotonic = step_deadline_monotonic
        # Laya 知识路由旁路（可选）：None 时知识检索行为与改造前一致。
        self._knowledge_laya_router = knowledge_laya_router
        # 互联网检索开关（与 manifest 冻结时同值，见 build(web_search=...)）。
        self.web_search_enabled = bool(web_search_enabled)
        self.run_id = str(run_id or new_id("hrun"))
        self.workspace_root = _workspace_root(
            tenant_id, session.id, task_frame_id, db=self.db
        )
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self._workspace_snapshot = snapshot_harness_workspace(self.workspace_root)
        ui_config = self.db.get(UIConfig, tenant_id)
        sandbox_enabled = bool(getattr(ui_config, "sandbox_enabled", False))
        sandbox_mode = parse_network_policy(
            getattr(ui_config, "sandbox_network_mode", None) if ui_config else None
        )
        sandbox_domains = tuple(
            str(item).strip()
            for item in (getattr(ui_config, "sandbox_allowed_domains", []) if ui_config else [])
            if str(item).strip()
        )
        self._file_registry = build_file_tool_registry()
        register_command_tools(self._file_registry)
        register_skill_script_tools(self._file_registry)
        self._file_executor = HarnessExecutor(self._file_registry)
        self._file_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=tenant_id,
            workspace_root=self.workspace_root,
            sandbox_enabled=sandbox_enabled,
            sandbox_network_mode=sandbox_mode,
            sandbox_allowed_domains=sandbox_domains,
        )
        self._sandbox_network_mode = sandbox_mode
        self._sandbox_allowed_domains = sandbox_domains
        self._sandbox_enabled = sandbox_enabled
        self._descriptors = {
            item.name: item
            for item in manifest.available
            if item.available
        }
        # ``None`` preserves compatibility for trusted direct callers.  The
        # Harness v2 engine always supplies the projected model allowlist so a
        # guessed hidden name cannot bypass progressive disclosure.
        self._activated_names = (
            set(self._descriptors)
            if initially_activated_names is None
            else {
                name
                for name in initially_activated_names
                if name in self._descriptors
            }
        )
        # 知识路由（document_route/bucket_route）是轻量分类任务，优先用
        # 意图识别专用模型（flash），未配置时回退主模型。解析一次缓存。
        self._routing_model_config = self._resolve_routing_model()

    @property
    def available_capability_names(self) -> set[str]:
        """当前冻结清单里可调用的能力名（含未 describe 的）。

        代码直调方（确定性知识工作流）用它判断「某个能力是否存在」，
        而不是探测私有字段。注意这只代表清单里有，调用仍受激活名单约束。
        """

        return set(self._descriptors)

    def _resolve_routing_model(self) -> ModelConfig | None:
        """意图识别轻量模型用于知识路由；失败/未配置回退主模型（None=主模型）。"""
        tenant_id = str(getattr(self.model_config, "tenant_id", "") or self.tenant_id)
        try:
            from app.agents.branching import model_for_agent

            routing_model = model_for_agent(
                self.db,
                tenant_id,
                self.agent_id,
                "intent_recognition",
            )
            if routing_model is not None:
                return routing_model
        except Exception:
            pass
        return self.model_config

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        inline_result: bool = False,
    ) -> dict[str, Any]:
        """执行一次能力调用。

        ``inline_result``：把自己的 ``data`` 无条件保留在返回值里，不做大载荷外置
        （``_persist_large_json_result``）。外置的目的只有一个 —— 别把超大的 JSON
        塞进**模型的 transcript**（模型读不全就只能 ``read_file`` 盲读、换词重搜）。
        但代码直调的调用方（确定性知识工作流）根本不进 transcript，它要的就是进程
        内的 dict，外置只会让它拿到一个 ``kind=sandbox_json_file`` 指针而判定「检索
        结果不可用」，白白降级回 Harness 决策循环（实测多出 3 次 LLM 调用）。
        因此这类调用方显式传 ``True`` 跳过外置，与预算是否够用彻底解耦。
        """
        self._raise_if_cancelled()
        if callable(self.ensure_execution_lease):
            self.ensure_execution_lease()
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            return _failure(
                "TOOL_NOT_AVAILABLE",
                "该能力不在当前 TaskFrame 的冻结清单中。",
            )
        if name not in self._activated_names:
            return _failure(
                "CAPABILITY_NOT_ACTIVATED",
                "该能力尚未在当前 AgentLoop 中展开；请先调用 capability_describe。",
            )
        current_descriptor = self._currently_authorized_descriptor(descriptor)
        if current_descriptor is None:
            return _failure(
                "CAPABILITY_AUTHORIZATION_REVOKED",
                "该能力在当前 HarnessRun 执行前已被撤权、归档或改为不可用。",
            )
        self._raise_if_cancelled()
        logical_action_key = self._logical_action_key(
            descriptor,
            arguments,
        )
        if logical_action_key:
            replayed = self._replay_or_block(logical_action_key)
            if replayed is not None:
                return replayed
        call_id = new_id("hcall")
        invocation = HarnessInvocationRecord(
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            task_id=self.task_frame_id,
            run_id=self.run_id,
            call_id=call_id,
            tool_name=name,
            request_digest=_request_digest(name, arguments),
            logical_action_key=logical_action_key,
            status="started",
            arguments_json=_audit_arguments(arguments),
        )
        self.db.add(invocation)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if logical_action_key:
                replayed = self._replay_or_block(logical_action_key)
                if replayed is not None:
                    return replayed
            raise
        try:
            self._raise_if_cancelled()
            if descriptor.kind == "internal":
                result = self._invoke_internal(name, arguments)
            elif descriptor.kind == "file":
                result = self._invoke_file(name, arguments, call_id=call_id)
            elif descriptor.kind == "general_skill":
                result = self._invoke_general_skill(
                    descriptor.capability_id,
                    descriptor.metadata,
                    arguments,
                )
            elif descriptor.kind == "knowledge":
                result = self._search_knowledge(
                    _intersect_knowledge_metadata(
                        descriptor.metadata,
                        current_descriptor.metadata,
                    ),
                    arguments,
                    call_id=call_id,
                    inline_result=inline_result,
                )
            elif descriptor.kind == "tool":
                result = self._invoke_external_tool(
                    descriptor.capability_id,
                    descriptor.metadata,
                    name,
                    arguments,
                    call_id=call_id,
                )
            else:
                result = _failure(
                    "UNSUPPORTED_CAPABILITY", "不支持的 Harness 能力类型。"
                )
        except HarnessExecutionCancelled:
            invocation.status = "cancelled"
            invocation.logical_action_key = None
            invocation.finished_at = utc_now()
            invocation.updated_at = utc_now()
            self.db.add(invocation)
            self.db.commit()
            raise
        except Exception as exc:
            result = _failure("HARNESS_TOOL_ERROR", str(exc))
        if result.get("success") is True:
            invocation.status = "completed"
        elif _failure_was_not_sent(result):
            # Configuration/authorization failures are known to occur before
            # the external side effect. Release the stable claim so a later
            # turn can retry after the configuration is repaired.
            invocation.status = "failed"
            invocation.logical_action_key = None
        else:
            # A timeout, HTTP error, connection reset, or MCP error can happen
            # after the provider accepted a write. Keep the claim and require
            # reconciliation instead of replaying the side effect.
            invocation.status = "outcome_unknown"
        invocation.result_json = _audit_result(result)
        invocation.response_cache_json = dict(result)
        invocation.finished_at = utc_now()
        invocation.updated_at = utc_now()
        self.db.add(invocation)
        self.db.commit()
        return result

    def discover_artifacts(self) -> list[dict[str, Any]]:
        """Publish every user-facing file changed during this AgentLoop run."""

        try:
            discovered = publish_changed_harness_artifacts(
                self.workspace_root,
                self.task_frame_id,
                self._workspace_snapshot,
                operation="workspace_discovery",
                path_filter=_is_user_facing_workspace_file,
            )
        except (HarnessArtifactAccessError, OSError):
            return []
        artifacts: list[dict[str, Any]] = []
        for raw in discovered:
            item = dict(raw)
            relative_path = str(item.get("path") or "")
            display_name = Path(relative_path).name
            item.update(
                {
                    "sandbox_path": _sandbox_path(relative_path),
                    "display_name": display_name,
                    "content_type": (
                        mimetypes.guess_type(display_name)[0]
                        or "application/octet-stream"
                    ),
                    "source": "harness.workspace_discovery",
                }
            )
            artifacts.append(item)
        return artifacts

    def _logical_action_key(
        self,
        descriptor: CapabilityDescriptor,
        arguments: dict[str, Any],
    ) -> str | None:
        if descriptor.kind != "tool":
            return None
        tool = self.db.get(Tool, descriptor.capability_id)
        if tool is None or tool.tenant_id != self.tenant_id:
            return None
        configured, key_fields = ToolReplayPolicy.configuration(
            tool.config_json if isinstance(tool.config_json, dict) else {},
            tool.input_schema if isinstance(tool.input_schema, dict) else {},
        )
        if configured is False:
            return None
        if configured is not True and not ToolReplayPolicy.default_replay_enabled(
            str(tool.method or "")
        ):
            return None
        key_arguments = ToolReplayPolicy.arguments(arguments, key_fields)
        signature = ToolReplayPolicy.signature(tool.name, key_arguments)
        canonical = json.dumps(
            {
                "tenant_id": self.tenant_id,
                "task_frame_id": self.task_frame_id,
                "step_id": self.active_step_id,
                "tool_id": tool.id,
                "signature": signature,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _replay_or_block(
        self,
        logical_action_key: str,
    ) -> dict[str, Any] | None:
        prior = self.db.exec(
            select(HarnessInvocationRecord).where(
                HarnessInvocationRecord.logical_action_key
                == logical_action_key
            )
        ).first()
        if prior is None:
            return None
        if (
            prior.status == "completed"
            and prior.response_cache_json.get("success") is True
        ):
            return _replayed_result(prior)
        return _failure(
            "TOOL_CALL_OUTCOME_UNKNOWN",
            (
                "相同副作用调用已有未完成的持久化记录；为避免重复提交，"
                "Harness 不会自动重试，请先核对外部系统状态。"
            ),
        )

    def _raise_if_cancelled(self) -> None:
        if callable(self.is_cancelled) and self.is_cancelled():
            raise HarnessExecutionCancelled(
                "Harness execution was cancelled before a capability call."
            )

    def _currently_authorized_descriptor(
        self,
        frozen: CapabilityDescriptor,
    ) -> CapabilityDescriptor | None:
        try:
            current = CapabilityManifestBuilder(self.db).build(
                self.tenant_id,
                self.agent_id,
                self.active_skill,
                self.active_step_id,
                web_search=self.web_search_enabled,
            )
        except CapabilityAuthorizationError:
            return None
        return next(
            (
                item
                for item in current.available
                if item.available
                and item.capability_id == frozen.capability_id
                and item.name == frozen.name
                and item.kind == frozen.kind
            ),
            None,
        )

    def _emit_trace(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if callable(self.trace_sink):
            self.trace_sink(event_type, payload)

    def _search_knowledge(
        self,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
        *,
        call_id: str,
        inline_result: bool = False,
    ) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "知识检索 query 不能为空。")
        allowed = {
            str(item)
            for item in metadata.get("allowed_knowledge_base_ids") or []
            if str(item).strip()
        }
        requested = {
            str(item)
            for item in arguments.get("knowledge_base_ids") or []
            if str(item).strip()
        }
        selected = sorted(requested & allowed) if requested else sorted(allowed)
        if requested and not selected:
            return _failure(
                "KNOWLEDGE_NOT_AVAILABLE",
                "请求的知识库不在当前 TaskFrame 授权范围内。",
            )
        version_by_base = (
            metadata.get("knowledge_version_by_base_id")
            if isinstance(metadata.get("knowledge_version_by_base_id"), dict)
            else {}
        )
        selected_version_ids = [
            str(version_by_base[kb_id])
            for kb_id in selected
            if str(version_by_base.get(kb_id) or "").strip()
        ]
        response = KnowledgeService(self.db, self._knowledge_laya_router).search(
            KnowledgeSearchRequest(
                tenant_id=self.tenant_id,
                agent_id=self.agent_id,
                query=query,
                mode="chat",
                knowledge_base_ids=selected,
                knowledge_base_version_ids=selected_version_ids,
                cache_query=self.user_message or None,
                max_chunks=max(
                    1, min(int(arguments.get("max_chunks") or 8), 12)
                ),
            ),
            self._routing_model_config,
        )
        payload = response.model_dump(mode="json")
        chunks = payload.get("chunks")
        # 完整 payload 只在进程内使用：引用卡片、回答上下文都由它派生，必须保持
        # 「全量证据」不变。交给模型与下游的 ``data`` 换成精简投影（见
        # app/knowledge/search_payload.py）——不精简就会被 _persist_large_json_result
        # 换成沙箱文件指针，模型读不全 160KB JSON，只能换词重搜（实测多花两轮）。
        # 内联预算按命中条数自适应：命中 15~20 条时固定 24K 塞不下，会被
        # _persist_large_json_result 换成沙箱文件指针（模型只能 read_file 盲读）。
        # 压缩与落盘判定必须共用同一个预算。
        # 反过来，``inline_result=True`` 的调用方（代码直调的知识工作流）不吃
        # transcript 预算，压到 24K 只会把证据正文砍到 200 字下限：它拿到的是一手
        # 证据，之后还要交给作答模型，正文被砍短就答不全、被迫 read_knowledge_chunk
        # 补读。这类调用方直接给硬上限，让证据保持完整。
        payload_budget = (
            KNOWLEDGE_PAYLOAD_HARD_MAX_CHARS
            if inline_result
            else knowledge_payload_budget(payload)
        )
        compacted = compact_knowledge_search_payload(payload, max_chars=payload_budget)
        result = {
            "success": True,
            "data": compacted,
            # 命中数外置到顶层：data 超限会被 _persist_large_json_result 换成
            # 沙箱文件引用，SOP 执行器（场景 C2 检索直判）靠它判断「检索到/没检索到」
            "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
            "citations": knowledge_citations_from_results([payload]),
        }
        # route 观测数据要在 data 外置（_persist_large_json_result 会把超限的
        # data 换成沙箱文件引用）之前提到顶层，否则 harness_tool_completed
        # 事件里永远看不到 route_cache_hit / fast_path 等 phase
        route_trace = payload.get("route_trace")
        if isinstance(route_trace, list) and route_trace:
            result["route_phases"] = [
                str(item.get("phase") or "?")
                for item in route_trace
                if isinstance(item, dict)
            ]
        raw_chars = serialized_chars(payload)
        inline_chars = serialized_chars(compacted)
        self._emit_trace(
            "knowledge_payload_compacted",
            {
                "query": query,
                "raw_chars": raw_chars,
                "inline_chars": inline_chars,
                "budget_chars": payload_budget,
                "chunk_count": result["chunk_count"],
                "evidence_count": len(compacted.get("evidence_pack") or []),
                "inline": inline_chars <= payload_budget,
            },
        )
        return self._persist_large_json_result(
            result,
            call_id=call_id,
            inline_max_chars=payload_budget,
            skip=inline_result,
        )

    def _invoke_external_tool(
        self,
        capability_id: str,
        metadata: dict[str, Any],
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> dict[str, Any]:
        source_tool_name = str(
            metadata.get("source_tool_name") or name
        ).strip()
        tool = self.db.get(Tool, capability_id)
        if (
            tool is None
            or tool.tenant_id != self.tenant_id
            or tool.name != source_tool_name
        ):
            return _failure(
                "TOOL_NOT_AVAILABLE",
                "工具在当前 HarnessRun 中已不可用。",
            )
        if tool_snapshot_digest(self.db, tool) != str(
            metadata.get("content_digest") or ""
        ):
            return _failure(
                "CAPABILITY_SNAPSHOT_CHANGED",
                "工具配置在当前 HarnessRun 启动后发生变化，请重新规划。",
            )
        try:
            resolved_arguments = self._resolve_json_tool_result_references(
                arguments,
                schema=(tool.input_schema if isinstance(tool.input_schema, dict) else None),
            )
        except HarnessExecutionError as exc:
            return _failure(
                exc.error.code,
                exc.error.message,
                retryable=exc.error.retryable,
                details=dict(exc.error.details),
            )
        result = ToolExecutor(self.db).execute(
            self.tenant_id,
            ToolCall(name=source_tool_name, arguments=resolved_arguments),
            active_skill_id=self.active_skill_id,
            agent_id=self.agent_id,
            session_id=self.session.id,
            invocation_id=call_id,
            timeout_seconds_override=self._remaining_step_seconds(),
        )
        payload = result.model_dump(mode="json")
        # MCP Apps payloads belong to the host UI, not to the isolated model
        # transcript. Emit a dedicated trace event so the frontend receives
        # the complete descriptor while the model only receives tool data.
        app_descriptor = payload.pop("mcp_app", None)
        payload.pop("mcp_metadata", None)
        if payload.get("success") is not True:
            return payload
        a2a_artifacts = self._materialize_a2a_artifacts(payload, call_id=call_id)
        if a2a_artifacts:
            payload["artifacts"] = [*(payload.get("artifacts") or []), *a2a_artifacts]
        payload = self._persist_large_json_result(payload, call_id=call_id)
        if isinstance(app_descriptor, dict) and payload.get("success") is True:
            app_descriptor["initial_result"] = payload.get("data")
            self._emit_trace(
                "harness_mcp_app_view",
                {
                    "tool_name": name,
                    "mcp_app": app_descriptor,
                },
            )
        return payload

    def _remaining_step_seconds(self) -> float | None:
        if self.step_deadline_monotonic is None:
            return None
        return max(self.step_deadline_monotonic - time.monotonic(), 0.1)


