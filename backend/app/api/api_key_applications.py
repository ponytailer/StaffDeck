from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select

from app.aliyun_aigw import (
    AliyunApigError,
    get_apig_client,
    get_gateway_config,
    get_gateway_configs,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import ApiKeyApplication, ApiKeyConsumerGroup, ApiKeyQuotaRule, User, utc_now
from app.security.auth import get_current_user
from app.security.encryption import (
    decrypt_secret,
    encrypt_secret,
    mask_secret,
    try_decrypt_secret,
)
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
_VALID_PERIODS = {"day", "week", "month"}


class ApiKeyApplicationCreate(BaseModel):
    tenant_id: str
    purpose: str | None = None


class ApiKeyApplicationReview(BaseModel):
    tenant_id: str
    reviewer_note: str | None = None


class ApiKeyApplicationApprove(BaseModel):
    tenant_id: str
    consumer_group_id: str  # 审批时选定的消费组(消费者)
    quota_rule_id: str  # 审批时选定的配额规则
    reviewer_note: str | None = None


class GatewayInfo(BaseModel):
    name: str
    gateway_id: str
    gateway_url: str


class ApiKeyConsumerGroupCreate(BaseModel):
    tenant_id: str
    name: str
    description: str | None = None
    owner: str | None = None  # 业务归属（非阿里云字段），取 CONSUMER_GROUP_OWNERS 配置值
    gateway_name: str  # 需在网关列表中存在


class ApiKeyConsumerGroupUpdate(BaseModel):
    tenant_id: str
    name: str | None = None  # 名称必填，端点内校验
    description: str | None = None
    owner: str | None = None  # 业务归属（非阿里云字段）


class ApiKeyConsumerGroupRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    description: str | None
    owner: str | None
    gateway_id: str | None
    gateway_name: str | None
    external_consumer_id: str | None
    consumer_type: str
    status: str
    created_by_user_id: str | None
    created_at: str
    updated_at: str


class ApiKeyQuotaRuleCreate(BaseModel):
    tenant_id: str
    name: str
    gateway_name: str
    quota_dimension: str = "token"  # token / credit
    quota_limit: int
    period_type: str = "day"  # day / week / month


class ApiKeyQuotaRuleUpdate(BaseModel):
    tenant_id: str
    name: str | None = None
    quota_limit: int | None = None
    period_type: str | None = None


class ApiKeyQuotaRuleRead(BaseModel):
    id: str
    tenant_id: str
    name: str
    gateway_id: str | None
    gateway_name: str | None
    quota_dimension: str
    quota_limit: int
    period_type: str
    external_rule_id: str | None
    status: str
    created_by_user_id: str | None
    created_at: str
    updated_at: str


class ConsumerGroupQuotaUpdate(BaseModel):
    tenant_id: str
    quota_rule_id: str  # 批量改配额:把该消费组纳入指定配额规则
    detach: bool = False  # True 表示从规则中移出该消费组


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
    gateway_name: str | None = None
    gateway_id: str | None = None
    quota_limit: int | None = None
    quota_period: str | None = None
    quota_rule_id: str | None = None
    quota_rule_name: str | None = None
    consumer_id: str | None = None
    consumer_name: str | None = None
    consumer_group_id: str | None = None
    consumer_group_name: str | None = None
    used_amount: int | None = None
    usage_month: str | None = None
    reviewer_note: str | None
    reviewed_at: str | None
    created_at: str
    updated_at: str


class ApiKeyApprovalStats(BaseModel):
    pending: int
    allocated: int
    history: int


class ApiKeyQuotaUpdate(BaseModel):
    tenant_id: str
    quota_limit: int


class ApiKeyApplicationUsageItem(BaseModel):
    id: str
    user_id: str
    username: str | None
    user_no: str | None = None
    department: str | None = None
    consumer_id: str | None = None
    consumer_name: str | None = None
    gateway_name: str | None = None
    gateway_id: str | None = None
    quota_limit: int | None = None
    quota_period: str | None = None
    quota_rule_id: str | None = None
    used_amount: int
    usage_rate: float  # 0.0 ~ 1.0
    suggestion: str  # expand / normal / watch / unknown


class ApiKeyUsageSummary(BaseModel):
    allocated_users: int
    total_quota: int
    total_used: int
    avg_usage_rate: float
    high_watermark_users: int
    low_watermark_users: int


class ApiKeyApplicationUsageRead(BaseModel):
    month: str
    summary: ApiKeyUsageSummary
    items: list[ApiKeyApplicationUsageItem]


def _read(row: ApiKeyApplication, include_secret: bool = False) -> ApiKeyApplicationRead:
    # 历史数据可能用旧 APP_SECRET 加密：容错解密，失败按「密钥失效」处理（masked 为空）
    plain_key = try_decrypt_secret(row.api_key_encrypted)
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
        gateway_name=row.gateway_name,
        gateway_id=row.gateway_id,
        quota_limit=row.quota_limit,
        quota_period=row.quota_period,
        quota_rule_id=row.quota_rule_id,
        quota_rule_name=row.quota_rule_name,
        consumer_id=row.consumer_id,
        consumer_name=row.consumer_name,
        consumer_group_id=row.consumer_group_id,
        consumer_group_name=row.consumer_group_name,
        used_amount=row.used_amount,
        usage_month=row.usage_month,
        reviewer_note=row.reviewer_note,
        reviewed_at=row.reviewed_at.isoformat() if row.reviewed_at else None,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _generate_api_key() -> str:
    """系统生成强随机 API Key 明文(作为阿里云消费者自定义凭证)。"""
    return "sk-" + secrets.token_urlsafe(32)


@router.get("/gateways", response_model=list[GatewayInfo])
def list_gateways() -> list[GatewayInfo]:
    """返回后端配置的网关列表(名称/gatewayId/调用地址),供审批时选择。"""
    try:
        configs = get_gateway_configs()
    except AliyunApigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return [
        GatewayInfo(name=cfg.name, gateway_id=cfg.gateway_id, gateway_url=cfg.gateway_url)
        for cfg in configs
    ]


@router.get("/stats", response_model=ApiKeyApprovalStats)
def approval_stats(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApprovalStats:
    """密钥审核页顶部统计:待审核 / 已分配 / 审核历史。"""
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyApplication).where(ApiKeyApplication.tenant_id == tenant_id)
    ).all()
    pending = sum(1 for r in rows if r.status == "pending")
    allocated = sum(1 for r in rows if r.status == "approved")
    history = sum(1 for r in rows if r.status in ("rejected", "revoked"))
    return ApiKeyApprovalStats(pending=pending, allocated=allocated, history=history)


@router.get("/consumer-group-owners", response_model=dict[str, list[str]])
def list_consumer_group_owners(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> dict[str, list[str]]:
    """返回消费组「归属」业务字段的可选值（来自 CONSUMER_GROUP_OWNERS 配置）。"""
    ensure_tenant_admin(tenant_id, current_user)
    return {"owners": get_settings().consumer_group_owner_list}


@router.get("/consumer-groups", response_model=list[ApiKeyConsumerGroupRead])
def list_consumer_groups(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyConsumerGroupRead]:
    """列出消费组(阿里云消费者)。"""
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyConsumerGroup)
        .where(ApiKeyConsumerGroup.tenant_id == tenant_id)
        .order_by(ApiKeyConsumerGroup.created_at.desc())
    ).all()
    return [_cg_read(r) for r in rows]


@router.post("/consumer-groups", response_model=ApiKeyConsumerGroupRead)
def create_consumer_group(
    request: ApiKeyConsumerGroupCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerGroupRead:
    """创建消费组:在指定网关下创建一个阿里云消费者。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    gateway = get_gateway_config(request.gateway_name)
    if not gateway:
        raise HTTPException(
            status_code=400,
            detail=f"未知网关名称「{request.gateway_name}」，请检查后端 ALIYUN_APIG_GATEWAYS 配置",
        )
    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="后端未配置阿里云 AK/SK（ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET），无法创建消费组",
        )
    try:
        consumer_id = client.create_consumer(
            name=request.name,
            description=request.description or "",
            gateway_type="AI",
        )
    except AliyunApigError as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row = ApiKeyConsumerGroup(
        tenant_id=request.tenant_id,
        name=request.name,
        description=(request.description or "").strip() or None,
        owner=(request.owner or "").strip() or None,
        gateway_id=gateway.gateway_id,
        gateway_name=gateway.name,
        external_consumer_id=consumer_id,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _cg_read(row)


@router.put("/consumer-groups/{group_id}", response_model=ApiKeyConsumerGroupRead)
def update_consumer_group(
    group_id: str,
    request: ApiKeyConsumerGroupUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerGroupRead:
    """编辑消费组:更新名称/描述/归属;描述变化时同步阿里云消费者(名称创建后不可改)。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyConsumerGroup, group_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="消费组不存在")
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="消费组名称不能为空")

    new_desc = (request.description or "").strip() or None
    old_desc = (row.description or "").strip() or None
    client = get_apig_client()
    if client is not None and row.external_consumer_id and new_desc != old_desc:
        try:
            client.update_consumer(row.external_consumer_id, description=new_desc or "")
        except AliyunApigError as exc:
            raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row.name = name
    row.description = new_desc
    row.owner = (request.owner or "").strip() or None
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _cg_read(row)


@router.delete("/consumer-groups/{group_id}", response_model=ApiKeyConsumerGroupRead)
def delete_consumer_group(
    group_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerGroupRead:
    """删除消费组:同步删除阿里云消费者。"""
    ensure_tenant_admin(tenant_id, current_user)
    row = db.get(ApiKeyConsumerGroup, group_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="消费组不存在")
    client = get_apig_client()
    if client is not None and row.external_consumer_id:
        try:
            client.delete_consumer(row.external_consumer_id)
        except AliyunApigError:
            pass  # 容忍删除失败,仍以本地删除为准
    db.delete(row)
    db.commit()
    return _cg_read(row)


@router.post("/consumer-groups/{group_id}/quota", response_model=ApiKeyConsumerGroupRead)
def consumer_group_change_quota(
    group_id: str,
    request: ConsumerGroupQuotaUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyConsumerGroupRead:
    """批量改配额:把消费组(消费者)加入/移出指定配额规则。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    group = db.get(ApiKeyConsumerGroup, group_id)
    if not group or group.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="消费组不存在")
    rule = db.get(ApiKeyQuotaRule, request.quota_rule_id)
    if not rule or rule.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="配额规则不存在")
    if group.gateway_id != rule.gateway_id:
        raise HTTPException(status_code=400, detail="消费组与配额规则不在同一网关")
    client = get_apig_client()
    if client is None:
        raise HTTPException(status_code=503, detail="后端未配置阿里云 AK/SK，无法批量改配额")
    try:
        if request.detach:
            client.detach_consumer_from_rule(
                gateway_id=rule.gateway_id,
                rule_id=rule.external_rule_id,
                consumer_id=group.external_consumer_id,
            )
        else:
            client.attach_consumer_to_rule(
                gateway_id=rule.gateway_id,
                rule_id=rule.external_rule_id,
                consumer_id=group.external_consumer_id,
            )
    except AliyunApigError as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc
    return _cg_read(group)


@router.get("/quota-rules", response_model=list[ApiKeyQuotaRuleRead])
def list_quota_rules(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[ApiKeyQuotaRuleRead]:
    """列出配额规则。"""
    ensure_tenant_admin(tenant_id, current_user)
    rows = db.exec(
        select(ApiKeyQuotaRule)
        .where(ApiKeyQuotaRule.tenant_id == tenant_id)
        .order_by(ApiKeyQuotaRule.created_at.desc())
    ).all()
    return [_qr_read(r) for r in rows]


@router.post("/quota-rules", response_model=ApiKeyQuotaRuleRead)
def create_quota_rule(
    request: ApiKeyQuotaRuleCreate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyQuotaRuleRead:
    """创建配额规则:在指定网关下创建一个阿里云 QuotaRule。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    if request.period_type not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail="period_type 必须是 day / week / month 之一")
    if not isinstance(request.quota_limit, int) or request.quota_limit <= 0:
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")
    gateway = get_gateway_config(request.gateway_name)
    if not gateway:
        raise HTTPException(
            status_code=400,
            detail=f"未知网关名称「{request.gateway_name}」，请检查后端 ALIYUN_APIG_GATEWAYS 配置",
        )
    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="后端未配置阿里云 AK/SK，无法创建配额规则",
        )
    try:
        rule_id = client.add_consumer_quota_rule(
            gateway_id=gateway.gateway_id,
            consumer_ids=[],
            quota_limit=request.quota_limit,
            period_type=request.period_type,
            rule_name=request.name,
            timezone="UTC+8",
        )
    except AliyunApigError as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row = ApiKeyQuotaRule(
        tenant_id=request.tenant_id,
        name=request.name,
        gateway_id=gateway.gateway_id,
        gateway_name=gateway.name,
        quota_dimension=request.quota_dimension,
        quota_limit=request.quota_limit,
        period_type=request.period_type,
        external_rule_id=rule_id,
        created_by_user_id=current_user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _qr_read(row)


@router.put("/quota-rules/{rule_id}", response_model=ApiKeyQuotaRuleRead)
def update_quota_rule_endpoint(
    rule_id: str,
    request: ApiKeyQuotaRuleUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyQuotaRuleRead:
    """编辑配额规则:更新规则名称/额度/周期(并同步阿里云)。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyQuotaRule, rule_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="配额规则不存在")
    if request.period_type and request.period_type not in _VALID_PERIODS:
        raise HTTPException(status_code=400, detail="period_type 必须是 day / week / month 之一")
    if request.quota_limit is not None and (not isinstance(request.quota_limit, int) or request.quota_limit <= 0):
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")

    client = get_apig_client()
    if client is None:
        raise HTTPException(status_code=503, detail="后端未配置阿里云 AK/SK，无法编辑配额规则")
    try:
        client.update_consumer_quota_rule(
            gateway_id=row.gateway_id,
            rule_id=row.external_rule_id,
            quota_limit=request.quota_limit if request.quota_limit is not None else row.quota_limit,
        )
        if request.name is not None or request.period_type is not None:
            client.update_quota_rule_meta(
                gateway_id=row.gateway_id,
                rule_id=row.external_rule_id,
                rule_name=request.name or row.name,
                period_type=request.period_type or row.period_type,
            )
    except AliyunApigError as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    if request.name is not None:
        row.name = request.name
    if request.quota_limit is not None:
        row.quota_limit = request.quota_limit
    if request.period_type is not None:
        row.period_type = request.period_type
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _qr_read(row)


@router.delete("/quota-rules/{rule_id}", response_model=ApiKeyQuotaRuleRead)
def delete_quota_rule(
    rule_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyQuotaRuleRead:
    """删除配额规则:同步删除阿里云 QuotaRule。"""
    ensure_tenant_admin(tenant_id, current_user)
    row = db.get(ApiKeyQuotaRule, rule_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="配额规则不存在")
    client = get_apig_client()
    if client is not None and row.external_rule_id:
        try:
            client.delete_quota_rule(row.external_rule_id, gateway_id=row.gateway_id)
        except AliyunApigError:
            pass
    db.delete(row)
    db.commit()
    return _qr_read(row)


def _cg_read(row: ApiKeyConsumerGroup) -> ApiKeyConsumerGroupRead:
    return ApiKeyConsumerGroupRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        owner=row.owner,
        gateway_id=row.gateway_id,
        gateway_name=row.gateway_name,
        external_consumer_id=row.external_consumer_id,
        consumer_type=row.consumer_type,
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _qr_read(row: ApiKeyQuotaRule) -> ApiKeyQuotaRuleRead:
    return ApiKeyQuotaRuleRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        gateway_id=row.gateway_id,
        gateway_name=row.gateway_name,
        quota_dimension=row.quota_dimension,
        quota_limit=row.quota_limit,
        period_type=row.period_type,
        external_rule_id=row.external_rule_id,
        status=row.status,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


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
    request: ApiKeyApplicationApprove,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    ensure_tenant_admin(request.tenant_id, current_user)
    row = _get_pending(db, application_id, request.tenant_id)

    # 1) 校验选定的消费组与配额规则
    group = db.get(ApiKeyConsumerGroup, request.consumer_group_id)
    if not group or group.tenant_id != request.tenant_id:
        raise HTTPException(status_code=400, detail="未知消费组")
    rule = db.get(ApiKeyQuotaRule, request.quota_rule_id)
    if not rule or rule.tenant_id != request.tenant_id:
        raise HTTPException(status_code=400, detail="未知配额规则")
    if group.gateway_id != rule.gateway_id:
        raise HTTPException(status_code=400, detail="消费组与配额规则不在同一网关")

    # 2) 阿里云客户端(未配置 AK/SK 直接报错)
    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="后端未配置阿里云 AK/SK（ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET），无法分配 API Key",
        )

    # 3) 生成自定义 API Key,并追加到消费组消费者凭证;把消费者纳入配额规则
    api_key = _generate_api_key()
    try:
        client.add_consumer_credential(
            consumer_id=group.external_consumer_id,
            api_key=api_key,
        )
        client.attach_consumer_to_rule(
            gateway_id=rule.gateway_id,
            rule_id=rule.external_rule_id,
            consumer_id=group.external_consumer_id,
        )
    except AliyunApigError as exc:
        raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    # 4) 落库
    row.api_key_encrypted = encrypt_secret(api_key)
    row.api_url = get_gateway_config(group.gateway_name).gateway_url if group.gateway_name else None
    row.gateway_name = group.gateway_name
    row.gateway_id = group.gateway_id
    row.quota_limit = int(rule.quota_limit)
    row.quota_period = rule.period_type
    row.quota_rule_id = rule.external_rule_id
    row.quota_rule_name = rule.name
    row.consumer_id = group.external_consumer_id
    row.consumer_name = group.name
    row.consumer_group_id = group.id
    row.consumer_group_name = group.name
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


def _current_month() -> str:
    return utc_now().strftime("%Y-%m")


def _coerce_str(value: str | None) -> str | None:
    """兼容 FastAPI Query 默认值对象（直接调用函数时可能传入 Query 对象）。"""
    if value is None or isinstance(value, str):
        return value
    return getattr(value, "default", None)


def _derive_user_no(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return f"{1000 + (hash(user_id) % 9000):04d}"


def _usage_suggestion(usage_rate: float) -> str:
    if usage_rate >= 0.9:
        return "expand"
    if usage_rate >= 0.7:
        return "watch"
    return "normal"


@router.get("/usage", response_model=ApiKeyApplicationUsageRead)
def list_usage(
    tenant_id: str = Query(...),
    month: str | None = Query(None, description="格式 YYYY-MM，缺省为当前月"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationUsageRead:
    """列出所有已签发 API Key 的用量明细与汇总（管理员）。"""
    ensure_tenant_admin(tenant_id, current_user)
    target_month = (_coerce_str(month) or _current_month()).strip()

    rows = db.exec(
        select(ApiKeyApplication).where(
            ApiKeyApplication.tenant_id == tenant_id,
            ApiKeyApplication.status == "approved",
        )
    ).all()

    client = get_apig_client()
    items: list[ApiKeyApplicationUsageItem] = []
    total_quota = 0
    total_used = 0
    high_count = 0
    low_count = 0

    for row in rows:
        # SQLite 历史迁移列可能以 VARCHAR 存储数值,统一转 int 防御
        quota_limit = int(row.quota_limit or 0)
        # 用量：优先使用当月缓存；否则尝试查询阿里云（mock/真实）
        used_amount = 0
        if row.usage_month == target_month and row.used_amount is not None:
            used_amount = row.used_amount
        elif client and row.gateway_id and row.quota_rule_id and row.consumer_id:
            try:
                resp = client.get_consumer_quota_usage(
                    gateway_id=row.gateway_id,
                    rule_id=row.quota_rule_id,
                    consumer_id=row.consumer_id,
                )
                data = resp.get("data") if isinstance(resp, dict) else {}
                used_amount = int(data.get("usedAmount") or 0)
                # 写回缓存（仅缓存当前月）
                if target_month == _current_month():
                    row.used_amount = used_amount
                    row.usage_month = target_month
                    row.updated_at = utc_now()
                    db.add(row)
            except AliyunApigError:
                used_amount = row.used_amount or 0
        else:
            used_amount = row.used_amount or 0

        usage_rate = (used_amount / quota_limit) if quota_limit > 0 else 0.0
        suggestion = _usage_suggestion(usage_rate)
        if usage_rate >= 0.9:
            high_count += 1
        elif usage_rate <= 0.4:
            low_count += 1

        total_quota += quota_limit
        total_used += used_amount

        items.append(
            ApiKeyApplicationUsageItem(
                id=row.id,
                user_id=row.user_id,
                username=row.username,
                user_no=_derive_user_no(row.user_id),
                department=None,
                consumer_id=row.consumer_id,
                consumer_name=row.consumer_name,
                gateway_name=row.gateway_name,
                gateway_id=row.gateway_id,
                quota_limit=quota_limit,
                quota_period=row.quota_period,
                quota_rule_id=row.quota_rule_id,
                used_amount=used_amount,
                usage_rate=round(usage_rate, 4),
                suggestion=suggestion,
            )
        )

    if target_month == _current_month():
        db.commit()

    allocated_users = len(rows)
    avg_usage_rate = (total_used / total_quota) if total_quota > 0 else 0.0

    return ApiKeyApplicationUsageRead(
        month=target_month,
        summary=ApiKeyUsageSummary(
            allocated_users=allocated_users,
            total_quota=total_quota,
            total_used=total_used,
            avg_usage_rate=round(avg_usage_rate, 4),
            high_watermark_users=high_count,
            low_watermark_users=low_count,
        ),
        items=items,
    )


@router.post("/{application_id}/quota", response_model=ApiKeyApplicationRead)
def update_quota(
    application_id: str,
    request: ApiKeyQuotaUpdate,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ApiKeyApplicationRead:
    """手动调整某条 API Key 的 Token 配额（管理员）。"""
    ensure_tenant_admin(request.tenant_id, current_user)
    row = db.get(ApiKeyApplication, application_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="API Key 申请不存在")
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="仅已批准的 API Key 可调整配额")
    if not isinstance(request.quota_limit, int) or request.quota_limit <= 0:
        raise HTTPException(status_code=400, detail="quota_limit 必须为正整数")

    client = get_apig_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="后端未配置阿里云 AK/SK，无法调整配额",
        )

    if row.gateway_id and row.quota_rule_id:
        try:
            client.update_consumer_quota_rule(
                gateway_id=row.gateway_id,
                rule_id=row.quota_rule_id,
                quota_limit=request.quota_limit,
            )
        except AliyunApigError as exc:
            raise HTTPException(status_code=502, detail=f"阿里云 APIG 调用失败：{exc}") from exc

    row.quota_limit = int(request.quota_limit)
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(row, include_secret=False)
