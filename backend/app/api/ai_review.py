"""AI Reviewer（代码评审）的企业端 API。

模块三层资源的租户级读写入口：

- **凭证**（``/credentials``）：GitHub / GitLab access token，租户级全局各一；
  只允许写入，读取永远只回掩码（``token_set`` + ``token_last4``）。
- **workspace**（``/workspaces``）：一个 workspace 对应一个待评审仓库；
  删除时级联清掉其下全部评审任务（批量 DELETE，见 ``app/db/bulk_delete.py``）。
- **预设**（``/presets``）：全局「review 要求」，提交任务时附加；``is_default``
  是单选语义——置默认会自动清掉同租户其他预设的默认位。
- **任务**（``/tasks``）：建任务时通过平台 API **实时**拉一次 PR/MR 详情并
  冻结快照（后续 MR 变更不影响本次评审口径），然后交给 rq 异步执行；
  列表 / 详情轮询状态与结果。

平台调用失败统一转 502（message 来自 ``PlatformError``，可直接给前端展示）。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import String, cast as sa_cast
from sqlmodel import Session, func, select

from app.ai_review.platform_client import (
    PLATFORMS,
    PlatformCredential,
    PlatformError,
    credential_display,
    get_merge_request,
    list_merge_requests,
    post_merge_request_comment,
    resolve_repo_url,
)
from app.ai_review.report_sync import build_review_report_markdown
from app.ai_review.rule_file import RuleFileError, parse_rule_file_config, sample_rule_file
from app.db import get_session
from app.db.bulk_delete import bulk_delete_matching, expunge_matching
from app.db.models import (
    AiReviewCredential,
    AiReviewPreset,
    AiReviewRuleFile,
    AiReviewTask,
    AiReviewWorkspace,
    User,
    utc_now,
)
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/ai-review",
    tags=["enterprise:ai-review"],
    dependencies=[Depends(get_current_user)],
)

MAX_PAGE_SIZE = 100


# --------------------------------------------------------------------------
# 请求体模型
# --------------------------------------------------------------------------


class CredentialUpsertRequest(BaseModel):
    tenant_id: str
    platform: str
    token: str = Field(max_length=500)
    # 仅 GitLab 自托管需要；github 侧忽略
    base_url: str = Field(default="", max_length=300)


class WorkspaceCreateRequest(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=100)
    platform: str
    repo_url: str = Field(min_length=1, max_length=500)
    default_branch: str = Field(default="main", max_length=200)


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    repo_url: str | None = Field(default=None, max_length=500)
    default_branch: str | None = Field(default=None, max_length=200)


class PresetCreateRequest(BaseModel):
    tenant_id: str
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=8000)
    is_default: bool = False


class PresetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    content: str | None = Field(default=None, max_length=8000)
    is_default: bool | None = None


class TaskCreateRequest(BaseModel):
    tenant_id: str
    workspace_id: str
    mr_number: int = Field(ge=1)
    # 附加评审要求（自由文本，可与预设叠加）
    requirements: str = Field(default="", max_length=4000)
    # 附加的全局预设 id（可选；内容按顺序拼接进 requirements 快照）
    preset_ids: list[str] = Field(default_factory=list, max_length=10)


class RuleFileUpsertRequest(BaseModel):
    tenant_id: str
    name: str = Field(default="", max_length=100)
    # JSON 字符串或结构化对象都收；结构校验见 ai_review.rule_file.parse_rule_file_config
    content: str | dict[str, Any]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _validate_platform(platform: str) -> str:
    platform = (platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise HTTPException(status_code=422, detail=f"不支持的平台：{platform}（可选 github / gitlab）")
    return platform


def _get_workspace(db: Session, tenant_id: str, workspace_id: str) -> AiReviewWorkspace:
    workspace = db.get(AiReviewWorkspace, workspace_id)
    if workspace is None or workspace.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Workspace 不存在")
    return workspace


def _get_tenant_credential(
    db: Session, tenant_id: str, platform: str
) -> AiReviewCredential | None:
    return (
        db.exec(
            select(AiReviewCredential).where(
                AiReviewCredential.tenant_id == tenant_id,
                AiReviewCredential.platform == platform,
            )
        )
        .first()
    )


def _platform_credential(row: AiReviewCredential) -> PlatformCredential:
    return PlatformCredential(platform=row.platform, token=row.token, base_url=row.base_url)


def _workspace_out(workspace: AiReviewWorkspace, task_count: int | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": workspace.id,
        "name": workspace.name,
        "platform": workspace.platform,
        "repo_url": workspace.repo_url,
        "repo_path": workspace.repo_path,
        "default_branch": workspace.default_branch,
        "created_at": workspace.created_at.isoformat() if workspace.created_at else None,
        "created_by": workspace.created_by,
    }
    if task_count is not None:
        data["task_count"] = task_count
    return data


def _preset_out(preset: AiReviewPreset) -> dict[str, Any]:
    return {
        "id": preset.id,
        "name": preset.name,
        "content": preset.content,
        "is_default": preset.is_default,
        "created_at": preset.created_at.isoformat() if preset.created_at else None,
        "updated_at": preset.updated_at.isoformat() if preset.updated_at else None,
    }


def _task_out(task: AiReviewTask, workspace_name: str = "") -> dict[str, Any]:
    return {
        "id": task.id,
        "workspace_id": task.workspace_id,
        "workspace_name": workspace_name,
        "status": task.status,
        "mr_number": task.mr_number,
        "mr_title": task.mr_title,
        "source_branch": task.source_branch,
        "target_branch": task.target_branch,
        "author": task.author,
        "web_url": task.web_url,
        "requirements": task.requirements,
        "error": task.error,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        # 结果回写平台评论区的同步记录（查看报告处展示）
        "platform_synced_at": (
            task.platform_synced_at.isoformat() if task.platform_synced_at else None
        ),
        "platform_sync_url": task.platform_sync_url,
        # 列表接口不带结果正文（result_json 可达上百条评论），详情接口才给
        "has_result": bool(task.result_json),
    }


def _apply_default_preset(db: Session, tenant_id: str, preset_id: str) -> None:
    """单选语义：把 ``preset_id`` 设为默认，同时清掉同租户其他预设的默认位。"""
    others = list(
        db.exec(
            select(AiReviewPreset).where(
                AiReviewPreset.tenant_id == tenant_id,
                AiReviewPreset.id != preset_id,
                AiReviewPreset.is_default == True,  # noqa: E712
            )
        ).all()
    )
    for row in others:
        row.is_default = False
        db.add(row)


def _compose_requirements(
    db: Session, tenant_id: str, requirements: str, preset_ids: list[str]
) -> str:
    """把自由文本要求与勾选的全局预设拼成最终口径（建任务时一次性冻结）。"""
    parts: list[str] = []
    text = (requirements or "").strip()
    if text:
        parts.append(text)
    for preset_id in preset_ids:
        preset = db.get(AiReviewPreset, preset_id)
        if preset is None or preset.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail=f"评审预设不存在：{preset_id}")
        parts.append(f"【全局评审要求：{preset.name}】\n{preset.content.strip()}")
    return "\n\n".join(parts).strip()


# --------------------------------------------------------------------------
# 凭证（租户级全局，token 只写不读）
# --------------------------------------------------------------------------


@router.get("/credentials")
def list_credentials(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    rows = {
        row.platform: row
        for row in db.exec(
            select(AiReviewCredential).where(AiReviewCredential.tenant_id == tenant_id)
        ).all()
    }
    results: list[dict[str, Any]] = []
    for platform in PLATFORMS:
        row = rows.get(platform)
        results.append(
            {
                "platform": platform,
                "base_url": (row.base_url if row else "") or "",
                **credential_display(row.token if row else ""),
            }
        )
    return results


@router.put("/credentials")
def upsert_credential(
    request: CredentialUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    platform = _validate_platform(request.platform)
    if platform == "github" and request.token.strip() == "":
        raise HTTPException(status_code=422, detail="GitHub token 不能为空")

    row = _get_tenant_credential(db, request.tenant_id, platform)
    if row is None:
        row = AiReviewCredential(tenant_id=request.tenant_id, platform=platform)
    # token 传空表示清除该平台凭证（base_url 照常可更新）
    row.token = request.token.strip()
    row.base_url = (request.base_url or "").strip().rstrip("/")
    if platform == "github":
        row.base_url = ""
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"platform": platform, "base_url": row.base_url, **credential_display(row.token)}


# --------------------------------------------------------------------------
# workspace
# --------------------------------------------------------------------------


@router.get("/workspaces")
def list_workspaces(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    workspaces = list(
        db.exec(
            select(AiReviewWorkspace)
            .where(AiReviewWorkspace.tenant_id == tenant_id)
            .order_by(AiReviewWorkspace.created_at.desc())  # type: ignore[union-attr]
        ).all()
    )
    counts: dict[str, int] = {}
    if workspaces:
        tasks = db.exec(
            select(AiReviewTask.workspace_id, AiReviewTask.id).where(
                AiReviewTask.tenant_id == tenant_id,
                AiReviewTask.workspace_id.in_([w.id for w in workspaces]),  # type: ignore[attr-defined]
            )
        ).all()
        for workspace_id, _task_id in tasks:
            counts[workspace_id] = counts.get(workspace_id, 0) + 1
    return [
        _workspace_out(workspace, counts.get(workspace.id, 0)) for workspace in workspaces
    ]


@router.post("/workspaces", status_code=201)
def create_workspace(
    request: WorkspaceCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    platform = _validate_platform(request.platform)
    try:
        repo_path, _clone_url = resolve_repo_url(platform, request.repo_url)
    except PlatformError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    duplicate = (
        db.exec(
            select(AiReviewWorkspace).where(
                AiReviewWorkspace.tenant_id == request.tenant_id,
                AiReviewWorkspace.platform == platform,
                AiReviewWorkspace.repo_url == request.repo_url.strip(),
            )
        ).first()
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该仓库已存在 workspace「{duplicate.name}」，无需重复创建",
        )

    workspace = AiReviewWorkspace(
        tenant_id=request.tenant_id,
        name=request.name.strip(),
        platform=platform,
        repo_url=request.repo_url.strip(),
        repo_path=repo_path,
        default_branch=(request.default_branch or "main").strip() or "main",
        created_by=current_user.username,
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return _workspace_out(workspace, 0)


@router.put("/workspaces/{workspace_id}")
def update_workspace(
    workspace_id: str,
    tenant_id: str = Query(...),
    request: WorkspaceUpdateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    workspace = _get_workspace(db, tenant_id, workspace_id)
    if request is not None:
        if request.name is not None and request.name.strip():
            workspace.name = request.name.strip()
        if request.default_branch is not None and request.default_branch.strip():
            workspace.default_branch = request.default_branch.strip()
        if request.repo_url is not None and request.repo_url.strip():
            new_url = request.repo_url.strip()
            if new_url != workspace.repo_url:
                try:
                    repo_path, _clone_url = resolve_repo_url(workspace.platform, new_url)
                except PlatformError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
                workspace.repo_url = new_url
                workspace.repo_path = repo_path
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return _workspace_out(workspace)


@router.delete("/workspaces/{workspace_id}")
def delete_workspace(
    workspace_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    workspace = _get_workspace(db, tenant_id, workspace_id)
    # 先摘 identity map 再批量删，避免 ObjectDeletedError（见 bulk_delete.py）
    expunge_matching(db, AiReviewWorkspace, id=workspace_id)
    bulk_delete_matching(db, AiReviewTask, workspace_id=workspace_id)
    bulk_delete_matching(db, AiReviewWorkspace, id=workspace_id)
    db.commit()
    return {"deleted": True, "id": workspace_id, "name": workspace.name}


# --------------------------------------------------------------------------
# PR/MR 列表（实时拉取）
# --------------------------------------------------------------------------


@router.get("/workspaces/{workspace_id}/merge-requests")
def list_workspace_merge_requests(
    workspace_id: str,
    tenant_id: str = Query(...),
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """open PR/MR 列表：服务端分页 + 搜索（标题 / 作者 / 编号，``#42`` 查编号）。

    平台分页粒度 100 条/页，``total`` 为 None 时表示仓库 open 量超过拉取上限，
    前端按 ``has_more`` 继续翻页。
    """
    _ensure_request_tenant(tenant_id, current_user)
    workspace = _get_workspace(db, tenant_id, workspace_id)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), MAX_PAGE_SIZE))
    credential_row = _get_tenant_credential(db, tenant_id, workspace.platform)
    if credential_row is None or not credential_row.token:
        raise HTTPException(
            status_code=400,
            detail=f"平台「{workspace.platform}」还没有配置 access token，请先到「平台设置」里填写。",
        )
    try:
        items, total, has_more = list_merge_requests(
            _platform_credential(credential_row), workspace.repo_path, page=page, page_size=page_size, search=search
        )
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "items": [item.to_dict() for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
    }


# --------------------------------------------------------------------------
# 评审要求预设（租户级全局）
# --------------------------------------------------------------------------


@router.get("/presets")
def list_presets(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    rows = list(
        db.exec(
            select(AiReviewPreset)
            .where(AiReviewPreset.tenant_id == tenant_id)
            .order_by(AiReviewPreset.updated_at.desc())  # type: ignore[union-attr]
        ).all()
    )
    return [_preset_out(row) for row in rows]


@router.post("/presets", status_code=201)
def create_preset(
    request: PresetCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    preset = AiReviewPreset(
        tenant_id=request.tenant_id,
        name=request.name.strip(),
        content=request.content.strip(),
        is_default=False,
    )
    db.add(preset)
    db.flush()
    if request.is_default:
        preset.is_default = True
        _apply_default_preset(db, request.tenant_id, preset.id)
        db.add(preset)
    db.commit()
    db.refresh(preset)
    return _preset_out(preset)


@router.put("/presets/{preset_id}")
def update_preset(
    preset_id: str,
    tenant_id: str = Query(...),
    request: PresetUpdateRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    preset = db.get(AiReviewPreset, preset_id)
    if preset is None or preset.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="评审预设不存在")
    if request is not None:
        if request.name is not None and request.name.strip():
            preset.name = request.name.strip()
        if request.content is not None and request.content.strip():
            preset.content = request.content.strip()
        if request.is_default is True:
            preset.is_default = True
            _apply_default_preset(db, tenant_id, preset.id)
        elif request.is_default is False:
            preset.is_default = False
        preset.updated_at = utc_now()
    db.add(preset)
    db.commit()
    db.refresh(preset)
    return _preset_out(preset)


@router.delete("/presets/{preset_id}")
def delete_preset(
    preset_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    preset = db.get(AiReviewPreset, preset_id)
    if preset is None or preset.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="评审预设不存在")
    expunge_matching(db, AiReviewPreset, id=preset_id)
    bulk_delete_matching(db, AiReviewPreset, id=preset_id)
    db.commit()
    return {"deleted": True, "id": preset_id}


# --------------------------------------------------------------------------
# 自定义评审规则文件（ocr rule.json，租户级一份）
# --------------------------------------------------------------------------


def _get_tenant_rule_file(db: Session, tenant_id: str) -> AiReviewRuleFile | None:
    return (
        db.exec(
            select(AiReviewRuleFile).where(AiReviewRuleFile.tenant_id == tenant_id)
        )
        .first()
    )


@router.get("/rules-file")
def get_rule_file(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """当前租户的自定义规则文件；未配置时给 ``exists=false`` 与一份示例结构。"""
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    row = _get_tenant_rule_file(db, tenant_id)
    if row is None:
        return {"exists": False, "name": "", "content": None, "sample": sample_rule_file()}
    return {
        "exists": True,
        "name": row.name,
        "content": row.content,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.put("/rules-file")
def upsert_rule_file(
    request: RuleFileUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """保存租户级自定义规则文件（结构非法直接 422，不会把坏 JSON 存下去）。"""
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    try:
        content = parse_rule_file_config(request.content)
    except RuleFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    row = _get_tenant_rule_file(db, request.tenant_id)
    if row is None:
        row = AiReviewRuleFile(tenant_id=request.tenant_id)
    row.name = request.name.strip() or "自定义评审规则"
    row.content = content
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "exists": True,
        "name": row.name,
        "content": row.content,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.delete("/rules-file")
def delete_rule_file(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """清空自定义规则：后续评审回落到项目/全局配置与系统内置规则。"""
    _ensure_request_tenant(tenant_id, current_user)
    row = _get_tenant_rule_file(db, tenant_id)
    if row is None:
        raise HTTPException(status_code=404, detail="还没有保存自定义规则文件")
    expunge_matching(db, AiReviewRuleFile, tenant_id=tenant_id)
    bulk_delete_matching(db, AiReviewRuleFile, tenant_id=tenant_id)
    db.commit()
    return {"deleted": True}


# --------------------------------------------------------------------------
# 评审任务
# --------------------------------------------------------------------------


@router.get("/tasks")
def list_tasks(
    tenant_id: str = Query(...),
    workspace_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """评审任务列表：分页 + 搜索（标题 / 编号 / 作者 / 要求快照）。

    返回 ``{items, total, page, page_size, total_pages}``；``total`` 精确（count 查询）。
    """
    _ensure_request_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), MAX_PAGE_SIZE))
    criteria: list[Any] = [AiReviewTask.tenant_id == tenant_id]
    if workspace_id:
        criteria.append(AiReviewTask.workspace_id == workspace_id)
    if status:
        criteria.append(AiReviewTask.status == status)
    search_norm = (search or "").strip()
    if search_norm:
        # #42 → 按编号精确查；其余按标题 / 编号 / 作者 / 要求快照模糊匹配
        if search_norm.startswith("#") and search_norm[1:].isdigit():
            criteria.append(AiReviewTask.mr_number == int(search_norm[1:]))
        else:
            keyword = f"%{search_norm.lower()}%"
            criteria.append(  # type: ignore[arg-type]
                func.lower(AiReviewTask.mr_title).like(keyword)
                | func.lower(AiReviewTask.author).like(keyword)
                | func.lower(AiReviewTask.requirements).like(keyword)
                | sa_cast(AiReviewTask.mr_number, String).like(keyword)
            )
    total_row = db.exec(
        select(func.count()).select_from(AiReviewTask).where(*criteria)  # type: ignore[arg-type]
    ).one()
    total = int(total_row or 0)
    total_pages = max(1, math.ceil(total / page_size))
    tasks = list(
        db.exec(
            select(AiReviewTask)
            .where(*criteria)  # type: ignore[arg-type]
            .order_by(AiReviewTask.created_at.desc())  # type: ignore[union-attr]
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    names: dict[str, str] = {}
    if tasks:
        workspaces = db.exec(
            select(AiReviewWorkspace).where(
                AiReviewWorkspace.tenant_id == tenant_id,
                AiReviewWorkspace.id.in_({task.workspace_id for task in tasks}),  # type: ignore[attr-defined]
            )
        ).all()
        names = {workspace.id: workspace.name for workspace in workspaces}
    return {
        "items": [_task_out(task, names.get(task.workspace_id, "")) for task in tasks],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    task = db.get(AiReviewTask, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="评审任务不存在")
    workspace = db.get(AiReviewWorkspace, task.workspace_id)
    data = _task_out(task, workspace.name if workspace else "")
    data["mr_description"] = task.mr_description
    data["result_json"] = task.result_json or []
    data["summary_json"] = task.summary_json or {}
    return data


@router.post("/tasks", status_code=201)
def create_task(
    request: TaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    workspace = _get_workspace(db, request.tenant_id, request.workspace_id)

    existing = (
        db.exec(
            select(AiReviewTask).where(
                AiReviewTask.tenant_id == request.tenant_id,
                AiReviewTask.workspace_id == workspace.id,
                AiReviewTask.mr_number == request.mr_number,
                AiReviewTask.status.in_(["queued", "running"]),  # type: ignore[attr-defined]
            )
        ).first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"PR/MR #{request.mr_number} 已有进行中的评审任务（{existing.status}），请等它完成后再提交。",
        )

    credential_row = _get_tenant_credential(db, request.tenant_id, workspace.platform)
    if credential_row is None or not credential_row.token:
        raise HTTPException(
            status_code=400,
            detail=f"平台「{workspace.platform}」还没有配置 access token，请先到「平台设置」里填写。",
        )

    # 实时拉一次 MR 详情并冻结快照；失败直接 502，不落半截任务
    try:
        mr = get_merge_request(
            _platform_credential(credential_row), workspace.repo_path, request.mr_number
        )
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    requirements = _compose_requirements(
        db, request.tenant_id, request.requirements, request.preset_ids
    )
    task = AiReviewTask(
        tenant_id=request.tenant_id,
        workspace_id=workspace.id,
        status="queued",
        mr_number=mr.number,
        mr_title=mr.title,
        mr_description=mr.description,
        source_branch=mr.source_branch,
        target_branch=mr.target_branch,
        author=mr.author,
        web_url=mr.web_url,
        requirements=requirements,
        created_by=current_user.username,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    from app.ai_review.jobs import schedule_ai_review

    scheduled = schedule_ai_review(task.id, tenant_id=request.tenant_id)
    return {**_task_out(task, workspace.name), "scheduled": scheduled}


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """失败 / 卡死的任务重跑：快照不变，重新入队。"""
    _ensure_request_tenant(tenant_id, current_user)
    task = db.get(AiReviewTask, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="评审任务不存在")
    if task.status not in ("queued", "failed"):
        raise HTTPException(status_code=409, detail=f"任务当前状态为 {task.status}，不能重试")

    task.status = "queued"
    task.error = ""
    db.add(task)
    db.commit()

    from app.ai_review.jobs import schedule_ai_review

    scheduled = schedule_ai_review(task.id, tenant_id=tenant_id)
    workspace = db.get(AiReviewWorkspace, task.workspace_id)
    return {**_task_out(task, workspace.name if workspace else ""), "scheduled": scheduled}


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    _ensure_request_tenant(tenant_id, current_user)
    task = db.get(AiReviewTask, task_id)
    if task is None or task.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="评审任务不存在")
    task_name = task.mr_title
    expunge_matching(db, AiReviewTask, id=task_id)
    bulk_delete_matching(db, AiReviewTask, id=task_id)
    db.commit()
    return {"deleted": True, "id": task_id, "mr_title": task_name}


class SyncToPlatformRequest(BaseModel):
    tenant_id: str


@router.post("/tasks/{task_id}/sync-to-platform")
def sync_task_to_platform(
    task_id: str,
    request: SyncToPlatformRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """把已完成的评审结果回写到 PR/MR 评论区（每条任务可重复回写，覆盖式新增评论）。

    成功后记录 ``platform_synced_at`` / ``platform_sync_url``；重复回写只是新增
    一条平台评论（平台 API 无编辑既有评论的幂等键），前端会展示最近一次回写记录。
    """
    _ensure_request_tenant(request.tenant_id, current_user)
    task = db.get(AiReviewTask, task_id)
    if task is None or task.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="评审任务不存在")
    if not task.result_json:
        raise HTTPException(status_code=409, detail="任务还没有评审结果，不能回写")
    workspace = db.get(AiReviewWorkspace, task.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="任务对应的 workspace 已被删除")
    credential_row = _get_tenant_credential(db, request.tenant_id, workspace.platform)
    if credential_row is None or not credential_row.token:
        raise HTTPException(
            status_code=400,
            detail=f"平台「{workspace.platform}」还没有配置 access token，请先到「平台设置」里填写。",
        )

    body = build_review_report_markdown(
        {
            "mr_number": task.mr_number,
            "mr_title": task.mr_title,
            "source_branch": task.source_branch,
            "target_branch": task.target_branch,
        },
        task.result_json or [],
        task.summary_json or {},
    )
    try:
        link = post_merge_request_comment(
            _platform_credential(credential_row), workspace.repo_path, task.mr_number, body
        )
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    task.platform_synced_at = utc_now()
    task.platform_sync_url = link
    db.add(task)
    db.commit()
    return {
        "synced": True,
        "platform_synced_at": task.platform_synced_at.isoformat() if task.platform_synced_at else None,
        "platform_sync_url": task.platform_sync_url,
    }
