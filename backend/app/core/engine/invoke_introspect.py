"""能力信息类能力：交付物列表/读取、知识源读取、能力检索与描述（批次 4.8 自 invoke.py 拆出，mixin 化，零行为改动）。"""

from __future__ import annotations

import logging

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
from app.core.engine.invoke_support import (
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

logger = logging.getLogger(__name__)


class _CapabilityInfoMixin:
    def _list_published_deliverables(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_limit = arguments.get("limit", MAX_PUBLISHED_DELIVERABLES)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return _failure("INVALID_ARGUMENTS", "limit 必须是整数。")
        rows = list_published_deliverables(
            self.db,
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            query=str(arguments.get("query") or ""),
            limit=raw_limit,
            exclude_task_frame_id=self.task_frame_id,
        )
        return {
            "success": True,
            "data": {
                "deliverables": rows,
                "count": len(rows),
                "notice": (
                    "使用 read_published_deliverable 读取所需文件，"
                    "不要用 read_file 猜测旧工作区路径。"
                ),
            },
        }

    def _read_published_deliverable(self, arguments: dict[str, Any]) -> dict[str, Any]:
        task_frame_id = str(arguments.get("task_frame_id") or "").strip()
        path = str(arguments.get("path") or "").strip()
        if not task_frame_id or not path:
            return _failure("INVALID_ARGUMENTS", "task_frame_id 和 path 不能为空。")
        artifact = find_published_deliverable(
            self.db,
            tenant_id=self.tenant_id,
            session_id=self.session.id,
            task_frame_id=task_frame_id,
            path=path,
        )
        if artifact is None:
            return _failure(
                "PUBLISHED_DELIVERABLE_NOT_FOUND",
                "当前会话中没有该历史交付物。",
            )
        workspace_root = _workspace_root(
            self.tenant_id,
            self.session.id,
            task_frame_id,
            db=self.db,
        )
        opened = None
        try:
            opened = open_harness_artifact(workspace_root, path)
            actual_sha256 = opened.sha256()
            expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
            expected_size = artifact.get("size")
            if (
                (expected_sha256 and expected_sha256 != actual_sha256.lower())
                or (isinstance(expected_size, int) and expected_size != opened.size)
            ):
                return _failure(
                    "PUBLISHED_DELIVERABLE_CHANGED",
                    "历史交付物在发布后已发生变化，拒绝读取。",
                )
        except (HarnessArtifactAccessError, OSError):
            return _failure(
                "PUBLISHED_DELIVERABLE_NOT_FOUND",
                "历史交付物不存在或无法安全读取。",
            )
        finally:
            if opened is not None:
                opened.close()

        read_arguments = {
            key: arguments[key]
            for key in ("path", "offset", "max_bytes", "continuation_token")
            if key in arguments
        }
        read_context = HarnessToolContext(
            run_id=self.run_id,
            task_frame_id=task_frame_id,
            tenant_id=self.tenant_id,
            workspace_root=workspace_root,
            limits=self._file_context.limits,
            sandbox_enabled=self._file_context.sandbox_enabled,
            sandbox_network_mode=self._file_context.sandbox_network_mode,
            sandbox_allowed_domains=self._file_context.sandbox_allowed_domains,
        )
        result = self._file_executor.execute(
            read_context,
            HarnessToolCall(
                call_id=new_id("hcall"),
                name="read_file",
                arguments=read_arguments,
            ),
        )
        if not result.success:
            return {
                "success": False,
                "error": {
                    "code": (
                        result.error.code
                        if result.error
                        else "PUBLISHED_DELIVERABLE_READ_FAILED"
                    ),
                    "message": (
                        result.error.message if result.error else "历史交付物读取失败。"
                    ),
                    "retryable": bool(result.error.retryable) if result.error else False,
                    "details": dict(result.error.details) if result.error else {},
                },
            }
        data = dict(result.data or {})
        data.update(
            {
                "task_frame_id": task_frame_id,
                "display_name": artifact.get("display_name"),
                "published_at": artifact.get("published_at"),
            }
        )
        return {"success": True, "data": data}

    def _allowed_knowledge_base_ids(self) -> set[str]:
        """当前 TaskFrame 冻结清单里授权的知识库 id。

        ``read_knowledge_source`` 与 ``knowledge_search`` 共用同一份授权，
        避免「检索受限但按 id 能读全租户」的越权通道。
        """

        for descriptor in self._descriptors.values():
            if descriptor.kind != "knowledge":
                continue
            raw = (descriptor.metadata or {}).get("allowed_knowledge_base_ids")
            if not isinstance(raw, list):
                continue
            return {str(item).strip() for item in raw if str(item).strip()}
        return set()

    def _read_knowledge_source(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 id 取知识原文的窄接口。

        知识检索结果内联后正文会按预算裁剪；需要逐字引用、核对数字或看完整段落时，
        用本工具按 ``chunk_id`` / ``concept_id`` 取回原文，而不是去读整包 JSON。
        """

        allowed = self._allowed_knowledge_base_ids()
        if not allowed:
            return _failure(
                "KNOWLEDGE_NOT_AVAILABLE",
                "当前 TaskFrame 没有已授权的知识库。",
            )
        chunk_ids = _unique_non_empty(arguments.get("chunk_ids"))
        concept_ids = _unique_non_empty(arguments.get("concept_ids"))
        if not chunk_ids and not concept_ids:
            return _failure(
                "INVALID_ARGUMENTS",
                "chunk_ids 或 concept_ids 至少提供一个。",
            )
        raw_max_chars = arguments.get("max_chars")
        if raw_max_chars is None:
            max_chars = _KNOWLEDGE_SOURCE_READ_DEFAULT_CHARS
        elif isinstance(raw_max_chars, bool) or not isinstance(raw_max_chars, int):
            return _failure("INVALID_ARGUMENTS", "max_chars 必须是整数。")
        else:
            max_chars = max(200, min(raw_max_chars, _KNOWLEDGE_SOURCE_READ_MAX_CHARS))
        sources: list[dict[str, Any]] = []
        missing: list[str] = []
        for chunk_id in chunk_ids[:_KNOWLEDGE_SOURCE_READ_MAX_IDS]:
            row = self.db.get(KnowledgeChunk, chunk_id)
            if (
                row is None
                or row.tenant_id != self.tenant_id
                or str(row.knowledge_base_id or "") not in allowed
            ):
                missing.append(chunk_id)
                continue
            metadata = row.metadata_json or {}
            content = str(row.content or "")
            sources.append(
                {
                    "kind": "chunk",
                    "chunk_id": row.id,
                    "document_id": row.document_id,
                    "bucket_id": row.bucket_id,
                    "section_path": metadata.get("section_path"),
                    "title": metadata.get("section_title") or metadata.get("bucket_title"),
                    "page": metadata.get("section_title"),
                    "summary": row.summary,
                    "content": _clip_for_read(content, max_chars),
                    "truncated": len(content) > max_chars,
                    "content_chars": len(content),
                }
            )
        for concept_id in concept_ids[:_KNOWLEDGE_SOURCE_READ_MAX_IDS]:
            row = self.db.get(KnowledgeConcept, concept_id)
            if row is None:
                row = self.db.exec(
                    select(KnowledgeConcept)
                    .where(KnowledgeConcept.tenant_id == self.tenant_id)
                    .where(KnowledgeConcept.concept_id == concept_id)
                ).first()
            if (
                row is None
                or row.tenant_id != self.tenant_id
                or str(row.knowledge_base_id or "") not in allowed
            ):
                missing.append(concept_id)
                continue
            content = str(row.content_md or "")
            sources.append(
                {
                    "kind": "concept",
                    "concept_id": row.concept_id,
                    "document_id": row.document_id,
                    "title": row.title,
                    "summary": row.description,
                    "content": _clip_for_read(content, max_chars),
                    "truncated": len(content) > max_chars,
                    "content_chars": len(content),
                }
            )
        self._emit_trace(
            "knowledge_source_read",
            {
                "requested": len(chunk_ids) + len(concept_ids),
                "returned": len(sources),
                "missing": missing[:5],
            },
        )
        return {
            "success": True,
            "data": {
                "sources": sources,
                "missing": missing,
                "notice": (
                    "以上为知识库原文；引用时请使用 knowledge_search 返回的编号，"
                    "并保持原文措辞不要改写数字与专有名词。"
                ),
            },
        }

    def _search_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "capability_search query 不能为空。")
        raw_kinds = arguments.get("kinds")
        allowed_kinds = {"general_skill", "tool", "knowledge", "file"}
        kinds: set[str] | None = None
        if raw_kinds is not None:
            if not isinstance(raw_kinds, list) or any(
                not isinstance(item, str) or item not in allowed_kinds
                for item in raw_kinds
            ):
                return _failure(
                    "INVALID_ARGUMENTS",
                    "capability_search kinds 包含不支持的能力类型。",
                )
            kinds = set(raw_kinds)
        raw_limit = arguments.get("limit", 8)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_search limit 必须是整数。",
            )
        limit = max(1, min(raw_limit, CAPABILITY_SEARCH_MAX_RESULTS))
        matches = search_capability_descriptors(
            self._descriptors.values(),
            query,
            kinds=kinds,
            limit=limit,
        )
        payload = {
            "snapshot_revision": self.manifest.snapshot_revision,
            "query": query,
            "matches": [
                catalog_entry(item).model_dump(mode="json") for item in matches
            ],
            "match_count": len(matches),
            "notice": (
                "搜索结果仍未激活；选择后调用 capability_describe 加载完整 schema。"
            ),
        }
        self._emit_trace(
            "capability_search_completed",
            {
                "query": query,
                "kinds": sorted(kinds) if kinds else [],
                "match_count": len(matches),
                "matches": [item.name for item in matches],
            },
        )
        return {"success": True, "data": payload}

    def _describe_capabilities(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_refs = arguments.get("capabilities")
        if not isinstance(raw_refs, list) or not raw_refs or len(raw_refs) > 8:
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_describe capabilities 必须包含 1 到 8 个能力名称或 ID。",
            )
        refs = [str(item or "").strip() for item in raw_refs]
        if any(not item for item in refs) or len(set(refs)) != len(refs):
            return _failure(
                "INVALID_ARGUMENTS",
                "capability_describe capabilities 不能包含空值或重复项。",
            )
        by_ref = {
            ref: descriptor
            for descriptor in self._descriptors.values()
            for ref in (descriptor.capability_id, descriptor.name)
        }
        activated: list[dict[str, Any]] = []
        not_found: list[str] = []
        revoked: list[str] = []
        for ref in refs:
            descriptor = by_ref.get(ref)
            if descriptor is None or descriptor.kind == "internal":
                not_found.append(ref)
                continue
            if self._currently_authorized_descriptor(descriptor) is None:
                revoked.append(ref)
                continue
            activated.append(model_descriptor(descriptor).model_dump(mode="json"))
            self._activated_names.add(descriptor.name)
        self._emit_trace(
            "capability_described",
            {
                "requested": refs,
                "activated": [item["name"] for item in activated],
                "not_found": not_found,
                "revoked": revoked,
            },
        )
        if not activated:
            return _failure(
                "CAPABILITY_NOT_AVAILABLE",
                "请求的能力不存在或已不可用。",
            )
        return {
            "success": True,
            "data": {
                "snapshot_revision": self.manifest.snapshot_revision,
                "activated_capabilities": activated,
                "not_found": not_found,
                "revoked": revoked,
                "notice": "以上能力已在当前 TaskFrame AgentLoop 中激活。",
            },
        }
