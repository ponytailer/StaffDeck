"""能力调用辅助：沙箱/技能包路径、审计、回执与文件包实体化（批次 4.8 自 invoke.py 搬出）。

invoke.py 保持 re-export。KnowledgeService / ToolExecutor 打桩缝所在的
``_search_knowledge`` / ``_invoke_external_tool`` 留在 invoke.py。
"""

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
    compact_knowledge_search_payload,
    knowledge_payload_budget,
    serialized_chars,
)
from app.knowledge.service import KnowledgeService
from app.tools.tool_executor import ToolExecutor
from app.tools.tool_schema import ToolCall

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
# 互联网检索：Harness 内部能力，实现见 app/web_search。只在员工级开关打开时
# 才会出现在 CapabilityManifest 里（见 capability/manifest._web_search_enabled）。
_WEB_SEARCH_TOOL_NAME = "web_search"


def _workspace_root(
    tenant_id: str, session_id: str, task_frame_id: str, *, db: Session | None = None
) -> Path:
    return harness_task_workspace_path(
        tenant_id=tenant_id,
        session_id=session_id,
        task_frame_id=task_frame_id,
        db=db,
    )


def _intersect_knowledge_metadata(
    frozen: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    current_ids = {
        str(item)
        for item in current.get("allowed_knowledge_base_ids") or []
        if str(item).strip()
    }
    frozen_ids = [
        str(item)
        for item in frozen.get("allowed_knowledge_base_ids") or []
        if str(item).strip() and str(item) in current_ids
    ]
    version_by_base = (
        frozen.get("knowledge_version_by_base_id")
        if isinstance(frozen.get("knowledge_version_by_base_id"), dict)
        else {}
    )
    filtered_versions = {
        kb_id: str(version_by_base[kb_id])
        for kb_id in frozen_ids
        if str(version_by_base.get(kb_id) or "").strip()
    }
    return {
        **frozen,
        "allowed_knowledge_base_ids": frozen_ids,
        "allowed_knowledge_base_version_ids": list(filtered_versions.values()),
        "knowledge_version_by_base_id": filtered_versions,
    }


def _failure(code: str, message: str, **details: Any) -> dict[str, Any]:
    error = {
        "code": code,
        "message": message,
        "retryable": False,
    }
    error.update(details)
    return {
        "success": False,
        "error": error,
    }


def _unique_non_empty(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _clip_for_read(content: str, limit: int) -> str:
    text = str(content or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}…"


def _safe_artifact_label(
    value: Any,
    *,
    fallback: str,
    max_length: int,
) -> str:
    cleaned = "".join(
        character
        for character in str(value or fallback).strip()
        if ord(character) >= 32 and ord(character) != 127
    )
    return cleaned[:max_length] or fallback[:max_length]


def _skill_package_preview(
    skill: GeneralSkill,
    *,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    package = package_from_row(skill)
    remaining = max_chars
    files: list[dict[str, Any]] = []
    for item in package.files:
        content = str(item.content or "")
        preview = content[:remaining]
        remaining -= len(preview)
        files.append(
            {
                "path": item.path,
                "size": item.size,
                "mime_type": item.mime_type,
                "content_preview": preview,
                "truncated": len(preview) < len(content),
            }
        )
        if remaining <= 0:
            break
    return {
        "package_id": package.package_id,
        "version": package.version,
        "digest": package.digest,
        "entrypoint": package.entrypoint,
        "file_count": len(package.files),
        "files": files,
        "truncated": len(files) < len(package.files)
        or any(bool(item.get("truncated")) for item in files),
    }


def _materialize_general_skill_package(
    workspace_root: Path,
    package: Any,
) -> tuple[str, list[str]]:
    """Write an immutable GeneralSkill snapshot into the current TaskFrame workspace."""

    safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(package.slug or "skill")).strip(
        ".-"
    ) or "skill"
    digest_suffix = re.sub(r"[^a-fA-F0-9]", "", str(package.digest or ""))[:12]
    directory_name = safe_slug + (f"-{digest_suffix}" if digest_suffix else "")
    relative_root = f"{_GENERAL_SKILL_PACKAGE_DIRECTORY}/{directory_name}"
    target_root = workspace_root / relative_root
    workspace_resolved = workspace_root.resolve()
    target_resolved = target_root.resolve(strict=False)
    if not target_resolved.is_relative_to(workspace_resolved):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package path escapes the TaskFrame workspace.",
        )
    if target_root.exists() and (target_root.is_symlink() or not target_root.is_dir()):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package destination is not a safe directory.",
        )
    target_root.mkdir(parents=True, exist_ok=True)
    file_paths: list[str] = []
    for item in package.files:
        relative_path = _safe_general_skill_file_path(str(item.path or ""))
        target = target_root / relative_path
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise HarnessExecutionError(
                "INVALID_SKILL_PACKAGE",
                "GeneralSkill package contains an unsafe existing file path.",
                details={"path": relative_path},
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.parent.resolve().is_relative_to(workspace_resolved):
            raise HarnessExecutionError(
                "INVALID_SKILL_PACKAGE",
                "GeneralSkill package file escapes the TaskFrame workspace.",
                details={"path": relative_path},
            )
        target.write_text(str(item.content or ""), encoding="utf-8")
        file_paths.append(f"{relative_root}/{relative_path}")
    return relative_root, file_paths


def _safe_general_skill_file_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if (
        not parts
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part == ".." for part in parts)
        or any("\x00" in part for part in parts)
    ):
        raise HarnessExecutionError(
            "INVALID_SKILL_PACKAGE",
            "GeneralSkill package contains an unsafe file path.",
            details={"path": path},
        )
    return "/".join(parts)


def _failure_was_not_sent(result: dict[str, Any]) -> bool:
    error = result.get("error")
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    return code in {
        "NOT_FOUND",
        "DISABLED",
        "NOT_ALLOWED",
        "UNSUPPORTED_TOOL_TYPE",
        "TOOL_NOT_AVAILABLE",
        "CAPABILITY_AUTHORIZATION_REVOKED",
        "CAPABILITY_SNAPSHOT_CHANGED",
        "CAPABILITY_NOT_ACTIVATED",
        "CAPABILITY_NOT_AVAILABLE",
        "INVALID_ARGUMENTS",
    }


def _replayed_result(invocation: HarnessInvocationRecord) -> dict[str, Any]:
    result = dict(invocation.response_cache_json or {})
    data = result.get("data")
    replay_metadata = {
        "idempotent_replay": True,
        "replayed_from_invocation_id": invocation.id,
    }
    if isinstance(data, dict):
        result["data"] = {**data, **replay_metadata}
    else:
        result["data"] = {
            "result": data,
            **replay_metadata,
        }
    result["idempotent_replay"] = True
    return result


def _request_digest(name: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audit_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    audited: dict[str, Any] = {}
    for key, value in arguments.items():
        lowered = str(key).lower()
        if any(
            token in lowered
            for token in ("content", "secret", "token", "password", "api_key")
        ):
            audited[str(key)] = "<redacted>"
        else:
            audited[str(key)] = value
    return audited


def _audit_result(result: dict[str, Any]) -> dict[str, Any]:
    audited = dict(result)
    data = audited.get("data")
    if isinstance(data, dict):
        audited["data"] = {
            key: (
                "<redacted>"
                if str(key).lower() in {"content", "instructions", "stdout", "stderr"}
                else value
            )
            for key, value in data.items()
        }
    citations = audited.get("citations")
    if isinstance(citations, list):
        audited["citations"] = [
            {
                key: value
                for key, value in item.items()
                if key not in {"content", "excerpt"}
            }
            for item in citations
            if isinstance(item, dict)
        ]
    return audited


def _sandbox_path(relative_path: str) -> str:
    normalized = str(relative_path or "").strip().replace("\\", "/")
    if normalized == SANDBOX_WORKSPACE or normalized.startswith(
        f"{SANDBOX_WORKSPACE}/"
    ):
        return normalized
    if normalized in {"", "."}:
        return SANDBOX_WORKSPACE
    return f"{SANDBOX_WORKSPACE}/{normalized.lstrip('/')}"


def _safe_artifact_name(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in Path(str(value or "artifact").replace("\\", "/")).name
    ).strip(".-")
    return cleaned[:180] or "artifact"


def _model_visible_file_result(value: Any, *, key: str = "") -> Any:
    path_keys = {"path", "source_path", "destination_path", "cwd"}
    if isinstance(value, dict):
        return {
            item_key: _model_visible_file_result(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_model_visible_file_result(item, key=key) for item in value]
    if isinstance(value, str) and key in path_keys:
        return _sandbox_path(value)
    return value


def _command_nonzero_message(data: dict[str, Any], tool_name: str) -> str:
    exit_code = data.get("exit_code")
    subject = "脚本" if tool_name == "run_skill_script" else "命令"
    if isinstance(exit_code, int):
        return f"{subject}执行完成，但返回非零退出码（exit {exit_code}）。"
    return f"{subject}执行完成，但报告了失败状态。"


def _command_nonzero_hint(tool_name: str) -> str:
    if tool_name == "run_skill_script":
        tail = (
            "先读 stderr 判断是脚本自身失败还是输入不满足；脚本确定失败时，"
            "不要重复执行同一 argv，改为检查参数或改用 typed 文件工具。"
        )
    else:
        tail = (
            "先读 stdout/stderr 判断是否影响结论；若属预期，可在命令里显式兜底"
            "（例如 `ls attachments 2>/dev/null || true`），或用 typed 文件工具代替。"
        )
    return (
        "非零退出码只表示进程自己报告了失败：grep/find/ls 等工具在「没有匹配 / "
        "目标不存在」时同样返回非零（常见 exit 1/2），而 stdout 往往仍包含所需信息。"
        + tail
    )


def _command_timeout_message(data: dict[str, Any]) -> str:
    timeout_seconds = data.get("timeout_seconds")
    if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
        return f"受控进程执行超时（上限 {timeout_seconds:g} 秒）。"
    return "受控进程执行超时。"


def _is_user_facing_workspace_file(path: str) -> bool:
    parts = Path(path).parts
    if not parts:
        return False
    first = parts[0]
    if (
        first in {"attachments", ".harness"}
        or first.startswith("general_skill_")
    ):
        return False
    if any(
        part in {
            ".git",
            ".harness-trash",
            ".pytest_cache",
            "__pycache__",
            "node_modules",
        }
        or part.startswith(".tmp-")
        for part in parts
    ):
        return False
    return Path(path).suffix.lower() not in {".pyc", ".pyo", ".part", ".tmp"}
