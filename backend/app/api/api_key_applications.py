from __future__ import annotations

import os
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from app.db import get_session
from app.db.models import ApiKeyApplication, User, utc_now
from app.security.auth import get_current_user
from app.security.encryption import decrypt_secret, encrypt_secret, mask_secret
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/api-key-applications",
    tags=["enterprise:api-key-applications"],
    dependencies=[Depends(get_current_user)],
)

MAX_APPLICATIONS_PER_USER = 2
ACTIVE_STATUSES = ("pending", "approved")
# 审批列表排序:pending 优先,其余按时间倒序
_STATUS_ORDER = {"pending": 0, "approved": 1, "rejected": 2, "revoked": 3}

# 本地 mock 网关地址;后续对接阿里云 DashScope 时由云侧下发真实网关与 key。
MOCK_API_GATEWAY_BASE = os.getenv(
    "MOCK_API_GATEWAY_BASE", "https://gateway.fosun-ai.com/v1"
)


class ApiKeyApplicationCreate(BaseModel):
    tenant_id: str
    purpose: str | None = None


class ApiKeyApplicationReview(BaseModel):
    tenant_id: str
    reviewer_note: str | None = None


class ApiKeyApplicationRead(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    username: str | None
    purpose: str | None
    status: str
    api_key_masked: str | None
    api_key: str | None = None  # 仅申请人本人查看已批准申请时返回明文
    api_url: str | None = None
    reviewer_note: str | None
    reviewed_at: str | None
    created_at: str
    updated_at: str


def _read(row: ApiKeyApplication, include_secret: bool = False) -> ApiKeyApplicationRead:
    plain_key = decrypt_secret(row.api_key_encrypted) if row.api_key_encrypted else None
    return ApiKeyApplicationRead(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        username=row.username,
        purpose=row.purpose,
        status=row.status,
        api_key_masked=mask_secret(plain_key) if plain_key else None,
        api_key=plain_key if (include_secret and plain_key) else None,
        api_url=row.api_url,
        reviewer_note=row.reviewer_note,
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _generate_mock_credentials(application_id: str) -> tuple[str, str]:
    """本地 mock 生成 api_key + api_url;预留阿里云 DashScope 对接点。"""
    api_key = f"sk-fosun-{uuid4().hex}"
    api_url = f"{MOCK_API_GATEWAY_BASE}/{application_id}"
    return api_key, api_url


@router.post("", response_model=ApiKeyApplicationRead)
def create_application(
    request: ApiKeyApplicationCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant(db, request.tenant_id)
    active = db.exec(
        select(ApiKeyApplication).where(
            ApiKeyApplication.tenant_id == request.tenant_id,
            ApiKeyApplication.user_id == current_user.id,
            ApiKeyApplication.status.in_(ACTIVE_STATUSES),
        )
    ).all()
    if len(active) >= MAX_APPLICATIONS_PER_USER:
        raise HTTPException(
            status_code=409,
            detail=f"每位用户最多申请 {MAX_APPLICATIONS_PER_USER} 个 API Key（当前已有 {len(active)} 个待审/已批准）",
        )
    row = ApiKeyApplication(
        tenant_id=request.tenant_id,
        user_id=current_user.id,
        username=current_user.username,
        purpose=(request.purpose or "").strip() or None,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row)


@router.get("/mine", response_model=list[ApiKeyApplicationRead])
def list_my_applications(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyApplicationRead]:
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(ApiKeyApplication)
        .where(
            ApiKeyApplication.tenant_id == tenant_id,
            ApiKeyApplication.user_id == current_user.id,
        )
        .order_by(ApiKeyApplication.created_at.desc())
    ).all()
    return [_read(row, include_secret=True) for row in rows]


@router.get("", response_model=list[ApiKeyApplicationRead])
def list_applications(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyApplicationRead]:
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyApplication).where(ApiKeyApplication.tenant_id == tenant_id)
    ).all()
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r.status, 9), -r.created_at.timestamp()))
    return [_read(row, include_secret=False) for row in rows]


def _get_pending(db: Session, application_id: str, tenant_id: str) -> ApiKeyApplication:
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理，无法重复操作")
    return row


@router.post("/{application_id}/approve", response_model=ApiKeyApplicationRead)
def approve_application(
    application_id: str,
    request: ApiKeyApplicationReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_pending(db, application_id, request.tenant_id)
    api_key, api_url = _generate_mock_credentials(row.id)
    row.api_key_encrypted = encrypt_secret(api_key)
    row.api_url = api_url
    row.status = "approved"
    row.reviewer_user_id = current_user.id
    row.reviewer_note = (request.reviewer_note or "").strip() or None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)


@router.post("/{application_id}/reject", response_model=ApiKeyApplicationRead)
def reject_application(
    application_id: str,
    request: ApiKeyApplicationReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_pending(db, application_id, request.tenant_id)
    row.status = "rejected"
    row.reviewer_user_id = current_user.id
    row.reviewer_note = (request.reviewer_note or "").strip() or None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)


@router.post("/{application_id}/revoke", response_model=ApiKeyApplicationRead)
def revoke_application(
    application_id: str,
    request: ApiKeyApplicationReview,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status == "revoked":
        return _read(row, include_secret=False)
    row.status = "revoked"
    row.api_key_encrypted = None
    row.api_url = None
    row.reviewer_user_id = current_user.id
    row.reviewer_note = (request.reviewer_note or "").strip() or None
    row.reviewed_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)
