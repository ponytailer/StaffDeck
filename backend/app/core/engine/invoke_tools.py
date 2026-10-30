"""能力执行的工具/文件/技能包分支：file、internal、general_skill 及其产物与包读取（批次 4.8 自 invoke.py 拆出，mixin 化，零行为改动）。"""

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
    _WEB_SEARCH_TOOL_NAME,
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


class _ToolPackageMixin:
    def _invoke_file(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> dict[str, Any]:
        result = self._file_executor.execute(
            self._file_context,
            HarnessToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
            ),
        )
        if result.success:
            data = dict(result.data or {})
            if name in {"exec_command", "run_skill_script"} and data.get("ok") is not True:
                visible_data = _model_visible_file_result(data)
                timed_out = bool(data.get("timed_out"))
                return {
                    "success": False,
                    "data": visible_data,
                    "error": {
                        "code": (
                            "COMMAND_TIMEOUT"
                            if timed_out
                            else "COMMAND_EXIT_NONZERO"
                        ),
                        "message": (
                            _command_timeout_message(data)
                            if timed_out
                            else _command_nonzero_message(data, name)
                        ),
                        # hint 让模型（和前端轨迹）不必解析整包 JSON 就能判断下一步：
                        # 非零退出码在 grep/find/ls 这类「未命中即非零」的命令上很常见，
                        # 且 stdout 往往仍包含所需信息（真实现场：状态探测脚本 exit 2，
                        # stdout 已列出 workspace 结构，模型却把它当成工具失败重跑）。
                        "hint": (
                            "命令超过单次执行上限被中止，请拆分任务或缩短单次命令。"
                            if timed_out
                            else _command_nonzero_hint(name)
                        ),
                        "exit_code": data.get("exit_code"),
                        "retryable": False,
                        "details": visible_data,
                    },
                    "duration_ms": result.duration_ms,
                }
            artifacts: list[dict[str, Any]] = []
            if name == "publish_artifact":
                artifact_path = str(data.get("path") or "").strip()
                if artifact_path:
                    artifacts.append(
                        {
                            "type": "workspace_file",
                            "task_frame_id": self.task_frame_id,
                            "path": artifact_path,
                            "sandbox_path": _sandbox_path(artifact_path),
                            "sha256": data.get("sha256"),
                            "size": data.get("size"),
                            "display_name": data.get("display_name"),
                            "description": data.get("description"),
                            "content_type": data.get("content_type"),
                            "operation": "publish_artifact",
                            "source": "harness",
                        }
                    )
            elif name in {"write_file", "edit_file", "copy_file", "move_file"}:
                data["published"] = False
                data["publication_hint"] = (
                    "文件已写入隔离工作区；如需提供给用户下载，请在校验完成后"
                    "显式调用 publish_artifact。"
                )
            return {
                "success": True,
                "data": _model_visible_file_result(data),
                "artifacts": artifacts,
                "duration_ms": result.duration_ms,
            }
        return {
            "success": False,
            "error": {
                "code": result.error.code if result.error else "FILE_TOOL_ERROR",
                "message": (
                    result.error.message
                    if result.error
                    else "文件工具执行失败。"
                ),
                "retryable": bool(result.error.retryable) if result.error else False,
                "details": dict(result.error.details) if result.error else {},
            },
            "duration_ms": result.duration_ms,
        }

    def _invoke_internal(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if name == "capability_search":
            return self._search_capabilities(arguments)
        if name == "capability_describe":
            return self._describe_capabilities(arguments)
        if name == "list_published_deliverables":
            return self._list_published_deliverables(arguments)
        if name == "read_published_deliverable":
            return self._read_published_deliverable(arguments)
        if name == _KNOWLEDGE_SOURCE_READ_TOOL_NAME:
            return self._read_knowledge_source(arguments)
        if name == _WEB_SEARCH_TOOL_NAME:
            return self._search_web(arguments)
        return _failure(
            "UNSUPPORTED_INTERNAL_CAPABILITY",
            "不支持的 Harness 内部能力。",
        )

    def _invoke_general_skill(
        self,
        capability_id: str,
        metadata: dict[str, Any],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        skill = self.db.get(GeneralSkill, capability_id)
        if (
            skill is None
            or skill.tenant_id != self.tenant_id
            or skill.status != "published"
        ):
            return _failure(
                "SKILL_NOT_AVAILABLE",
                "通用技能在当前 HarnessRun 中已不可用。",
            )
        digest = general_skill_snapshot_digest(skill)
        if digest != str(metadata.get("content_digest") or ""):
            return _failure(
                "CAPABILITY_SNAPSHOT_CHANGED",
                "通用技能内容在当前 HarnessRun 启动后发生变化，请重新规划。",
            )
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _failure("INVALID_ARGUMENTS", "通用技能 query 不能为空。")
        # GeneralSkill is an instruction package.  Loading it enriches the same
        # isolated AgentLoop transcript; execution remains the responsibility of
        # the regular Harness tools.  Accept legacy ``execute`` calls as a safe
        # alias for ``read`` so persisted model/tool calls do not suddenly fail,
        # but never start the old generated-runner pipeline from business runs.
        requested_operation = str(arguments.get("operation") or "read").strip().lower()
        if requested_operation not in {"read", "execute"}:
            return _failure(
                "INVALID_ARGUMENTS",
                "通用技能 operation 只能是 read。",
            )
        result = self._read_general_skill_package(skill, metadata, query)
        if requested_operation == "execute":
            result["data"]["requested_operation"] = "execute"
            result["data"]["compatibility_notice"] = (
                "execute 已弃用并安全降级为 read；请按 SKILL.md 指导调用现有 Harness 工具。"
            )
        self._emit_trace(
            "general_skill_trace",
            {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                "operation": "read",
                "requested_operation": requested_operation,
                "phase": "instructions_loaded",
                "message": "已加载技能说明，AgentLoop 将按说明选择 Harness 工具",
            },
        )
        return result

    def _general_skill_artifacts(
        self,
        declared: list[dict[str, Any]],
        *,
        skill_slug: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        artifacts: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        for item in declared[:20]:
            path = str(item.get("path") or "").strip()
            if not path:
                warnings.append(
                    {
                        "path": "",
                        "code": "artifact_publish_failed",
                        "message": "Artifact path cannot be empty.",
                    }
                )
                continue
            opened = None
            try:
                opened = open_harness_artifact(self.workspace_root, path)
                digest = opened.sha256()
                display_name = _safe_artifact_label(
                    item.get("display_name"),
                    fallback=opened.filename,
                    max_length=180,
                )
                description = _safe_artifact_label(
                    item.get("description"),
                    fallback="",
                    max_length=500,
                )
                artifacts.append(
                    {
                        "type": "workspace_file",
                        "task_frame_id": self.task_frame_id,
                        "path": path,
                        "sandbox_path": _sandbox_path(path),
                        "sha256": digest,
                        "size": opened.size,
                        "display_name": display_name,
                        "description": description or None,
                        "content_type": (
                            mimetypes.guess_type(display_name)[0]
                            or mimetypes.guess_type(opened.filename)[0]
                            or "application/octet-stream"
                        ),
                        "operation": "general_skill.execute",
                        "source": f"general_skill.{skill_slug}",
                    }
                )
            except (HarnessArtifactAccessError, OSError) as exc:
                warnings.append(
                    {
                        "path": path,
                        "code": "artifact_publish_failed",
                        "message": str(exc),
                    }
                )
            finally:
                if opened is not None:
                    opened.close()
        if warnings:
            self._emit_trace(
                "general_skill_artifact_rejected",
                {"skill_slug": skill_slug, "warnings": warnings},
            )
        return artifacts, warnings

    def _read_general_skill_package(
        self,
        skill: GeneralSkill,
        metadata: dict[str, Any],
        query: str,
    ) -> dict[str, Any]:
        package = package_from_row(skill)
        package_root, file_paths = _materialize_general_skill_package(
            self.workspace_root,
            package,
        )
        entrypoint_path = next(
            (
                item
                for item in file_paths
                if item.removeprefix(package_root + "/") == package.entrypoint
            ),
            f"{package_root}/{package.entrypoint}",
        )
        return {
            "success": True,
            "data": {
                "kind": "general_skill",
                "slug": metadata.get("slug"),
                "operation": "read",
                "query": query,
                "package": _skill_package_preview(skill),
                "package_root": package_root,
                "sandbox_package_root": _sandbox_path(package_root),
                "entrypoint_path": entrypoint_path,
                "sandbox_entrypoint_path": _sandbox_path(entrypoint_path),
                "file_paths": file_paths,
                "notice": (
                    "技能包说明已加载到当前隔离 Harness transcript；"
                    "包内文件已物化到 package_root，请只使用返回的 entrypoint_path 和"
                    " file_paths 定位文件；"
                    "请由 AgentLoop 直接应用其中的 prompt、规则和示例，并按任务需要调用"
                    "知识库、原装 Tool、exec_command 或 typed 文件工具；Skill 本身不会"
                    "生成临时代码或启动第二套 runner。"
                ),
            },
        }

    def _materialize_a2a_artifacts(
        self,
        payload: dict[str, Any],
        *,
        call_id: str,
    ) -> list[dict[str, Any]]:
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("artifacts"), list):
            return []
        published: list[dict[str, Any]] = []
        directory = self.workspace_root / "artifacts" / "a2a" / _safe_artifact_name(call_id)
        for artifact_index, artifact in enumerate(data["artifacts"], start=1):
            if not isinstance(artifact, dict):
                continue
            for part_index, part in enumerate(artifact.get("parts") or [], start=1):
                if not isinstance(part, dict) or not isinstance(part.get("file"), dict):
                    continue
                file_part = part["file"]
                encoded = file_part.get("bytes")
                if not isinstance(encoded, str) or not encoded:
                    continue
                try:
                    content = base64.b64decode(encoded, validate=True)
                except ValueError:
                    continue
                requested_name = str(file_part.get("name") or "").strip()
                filename = _safe_artifact_name(
                    requested_name or f"artifact-{artifact_index}-{part_index}.bin"
                )
                directory.mkdir(parents=True, exist_ok=True)
                output = directory / filename
                suffix = 2
                while output.exists():
                    output = directory / f"{Path(filename).stem}-{suffix}{Path(filename).suffix}"
                    suffix += 1
                output.write_bytes(content)
                relative = output.relative_to(self.workspace_root).as_posix()
                sha256 = hashlib.sha256(content).hexdigest()
                content_type = str(file_part.get("mimeType") or "").strip() or (
                    mimetypes.guess_type(filename)[0] or "application/octet-stream"
                )
                file_part.pop("bytes", None)
                file_part["path"] = relative
                file_part["sandbox_path"] = _sandbox_path(relative)
                file_part["sha256"] = sha256
                published.append(
                    {
                        "type": "workspace_file",
                        "task_frame_id": self.task_frame_id,
                        "path": relative,
                        "sandbox_path": _sandbox_path(relative),
                        "sha256": sha256,
                        "size": len(content),
                        "display_name": requested_name or filename,
                        "description": str(artifact.get("description") or "A2A Artifact"),
                        "content_type": content_type,
                        "operation": "a2a_artifact",
                        "source": "a2a",
                    }
                )
        return published

    def _persist_large_json_result(
        self,
        payload: dict[str, Any],
        *,
        call_id: str,
        inline_max_chars: int = _INLINE_JSON_TOOL_RESULT_MAX_CHARS,
        skip: bool = False,
    ) -> dict[str, Any]:
        """Keep large knowledge/tool JSON out of the isolated model transcript.

        ``inline_max_chars`` 默认沿用 HTTP/MCP Tool 的 2000 字符口径；知识检索传入
        更高的预算（见 app/knowledge/search_payload.py），让精简后的载荷能直接内联，
        不必先落文件再让模型盲读。

        ``skip=True``：调用方已声明「这份结果不进模型 transcript」（代码直调的确定性
        知识工作流），此时无论多大都原样返回。外置只是为了保护 transcript；对不进
        transcript 的调用方，外置纯属自伤 —— 它会拿到 ``kind=sandbox_json_file``
        指针而判定「结果不可用」，从确定性路径降级回决策循环。
        """

        if skip:
            return payload
        data = payload.get("data")
        if not isinstance(data, (dict, list)):
            return payload
        try:
            serialized = json.dumps(
                data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return payload
        if len(serialized) <= inline_max_chars:
            return payload
        stored = self._file_executor.execute(
            self._file_context,
            HarnessToolCall(
                call_id=f"{call_id}-result",
                name="write_file",
                arguments={
                    "path": f"{_INTERNAL_TOOL_RESULT_DIRECTORY}/{call_id}.json",
                    "content": serialized,
                    "create_parents": True,
                },
            ),
        )
        if not stored.success:
            return _failure(
                "TOOL_RESULT_PERSIST_FAILED",
                "能力已返回结果，但完整 JSON 无法写入当前 TaskFrame 沙箱。",
                cause={
                    "code": (
                        stored.error.code
                        if stored.error is not None
                        else "FILE_TOOL_ERROR"
                    ),
                    "message": (
                        stored.error.message
                        if stored.error is not None
                        else "沙箱文件写入失败。"
                    ),
                },
            )
        stored_data = dict(stored.data or {})
        relative_path = str(stored_data.get("path") or "").strip()
        reference = {
            "kind": _SANDBOX_JSON_FILE_KIND,
            "sandbox_path": _sandbox_path(relative_path),
            "size": stored_data.get("size"),
            "sha256": stored_data.get("sha256"),
        }
        payload["data"] = reference
        app_descriptor = payload.get("mcp_app")
        if isinstance(app_descriptor, dict):
            app_descriptor["initial_result"] = dict(reference)
        return payload

    def _resolve_json_tool_result_references(
        self,
        value: Any,
        *,
        schema: dict[str, Any] | None = None,
        depth: int = 0,
    ) -> Any:
        if depth > 32:
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "工具参数中的 JSON 结果引用嵌套过深。",
            )
        if isinstance(value, list):
            item_schema = (
                schema.get("items")
                if isinstance(schema, dict) and isinstance(schema.get("items"), dict)
                else None
            )
            return [
                self._resolve_json_tool_result_references(
                    item,
                    schema=item_schema,
                    depth=depth + 1,
                )
                for item in value
            ]
        if not isinstance(value, dict):
            return value
        if value.get("kind") == _SANDBOX_JSON_FILE_KIND:
            resolved = self._read_json_tool_result_reference(value)
            if isinstance(schema, dict) and schema.get("type") == "string":
                return json.dumps(
                    resolved,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            return resolved
        properties = (
            schema.get("properties")
            if isinstance(schema, dict) and isinstance(schema.get("properties"), dict)
            else {}
        )
        return {
            key: self._resolve_json_tool_result_references(
                item,
                schema=(properties.get(key) if isinstance(properties.get(key), dict) else None),
                depth=depth + 1,
            )
            for key, item in value.items()
        }

    def _read_json_tool_result_reference(self, reference: dict[str, Any]) -> Any:
        sandbox_path = str(reference.get("sandbox_path") or "").strip()
        prefix = f"{SANDBOX_WORKSPACE}/"
        if not sandbox_path.startswith(prefix):
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "JSON 结果引用必须使用当前 TaskFrame 的 /workspace 沙箱路径。",
            )
        relative_path = sandbox_path[len(prefix) :]
        expected_prefix = f"{_INTERNAL_TOOL_RESULT_DIRECTORY}/"
        if (
            not relative_path.startswith(expected_prefix)
            or "/" in relative_path[len(expected_prefix) :]
            or not relative_path.endswith(".json")
        ):
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "JSON 结果引用不属于 Harness 管理的工具结果目录。",
            )
        try:
            opened = open_harness_artifact(self.workspace_root, relative_path)
        except HarnessArtifactAccessError as exc:
            raise HarnessExecutionError(
                "TOOL_RESULT_REFERENCE_UNAVAILABLE",
                "引用的 JSON 工具结果文件不存在或不可安全读取。",
            ) from exc
        try:
            if opened.size > self._file_context.limits.max_file_bytes:
                raise HarnessExecutionError(
                    "TOOL_RESULT_REFERENCE_TOO_LARGE",
                    "引用的 JSON 工具结果超过当前 Harness 单文件上限。",
                    details={
                        "actual_bytes": opened.size,
                        "max_bytes": self._file_context.limits.max_file_bytes,
                    },
                )
            expected_sha256 = str(reference.get("sha256") or "").strip().lower()
            actual_sha256 = opened.sha256()
            if expected_sha256 and expected_sha256 != actual_sha256:
                raise HarnessExecutionError(
                    "TOOL_RESULT_REFERENCE_CHANGED",
                    "引用的 JSON 工具结果文件已发生变化。",
                    details={
                        "expected_sha256": expected_sha256,
                        "actual_sha256": actual_sha256,
                    },
                )
            raw = b"".join(opened.iter_bytes())
        finally:
            opened.close()
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessExecutionError(
                "INVALID_TOOL_RESULT_REFERENCE",
                "引用的工具结果文件不是有效的 UTF-8 JSON。",
            ) from exc
