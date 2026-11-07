"""个人静态资源：上传后得到一个稳定的公开下载地址。

定位（和知识库划清边界）——
- 知识库：文件被解析成段落，喂给模型**读**；
- 这里：原文件原样给用户**下**，正文不进检索，只提供一个地址。

所以这个模块不碰知识库链路，只负责「存得住、取得到、地址不泄露内部结构」。

鉴权口径 —— 与 `general_skill_shares` 一致：
- 管理接口（上传 / 列表 / 删除）需要登录，且只能操作自己上传的资源；
- 下载端点 `/f/{token}/{filename}` **公开免登录**：地址会被贴进知识库、被模型回给
  用户，甚至转发到外部，要求登录就断在最后一步。token 不可枚举，删除后 410。
  这是有意的取舍：拿到链接即可下载。面板上必须提示「请勿上传敏感文件」。

安全（少一条都是漏洞）——
1. 所有下载强制 `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`；
2. MIME 按扩展名白名单判定，html/svg/js/xml 与未知类型一律降级 octet-stream；
3. 不回 `Access-Control-Allow-Origin`，否则任意网页可 fetch 读取内容；
4. URL 里的 filename 段只作展示，解析时忽略 —— 改名不会让老链接失效。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import StaticAsset, User, utc_now
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant
from app.static_assets.store import (
    STATIC_ASSET_MAX_BYTES,
    STATIC_ASSET_TENANT_QUOTA_BYTES,
    STATIC_ASSET_TOKEN_BYTES,
    STATIC_ASSET_USER_QUOTA_BYTES,
    content_type_for,
    delete_static_asset_file,
    read_static_asset,
    safe_download_name,
    sha256_of,
    write_static_asset,
)

router = APIRouter(prefix="/api/enterprise/static-assets", tags=["enterprise:static-assets"])

#: 公开下载端点刻意不挂在 /api 下：它是唯一不需要登录的读路径，
#: 前缀越明确越好排查（日志里一眼能认出来）。
public_router = APIRouter(tags=["static-assets:public"])

STATUS_ACTIVE = "active"
STATUS_DELETED = "deleted"


class StaticAssetRead(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    sha256: str
    status: str
    download_count: int
    created_at: datetime
    #: 相对下载路径。绝对地址由前端拼（域名换了历史链接自动跟着变，
    #: 后端落库绝对 URL 等于把域名写死进数据里）。
    path: str


class StaticAssetListRead(BaseModel):
    """列表 + 空间用量。

    空间信息跟着列表一起回，前端才能在上传前就显示「已用 / 上限」和单文件限制，
    而不是等用户传完一个 30MB 文件被 413 拦住才告诉他规则。
    """

    items: list[StaticAssetRead]
    used_bytes: int
    user_quota_bytes: int
    max_file_bytes: int


def _to_read(row: StaticAsset) -> StaticAssetRead:
    return StaticAssetRead(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size=row.size,
        sha256=row.sha256,
        status=row.status,
        download_count=row.download_count,
        created_at=row.created_at,
        path=f"/f/{row.token}/{quote(safe_download_name(row.filename), safe='')}",
    )


def _new_token(db: Session) -> str:
    """生成不与现有 token 冲突的短 token。"""
    for _ in range(8):
        token = secrets.token_urlsafe(STATIC_ASSET_TOKEN_BYTES)
        exists = db.exec(select(StaticAsset.id).where(StaticAsset.token == token)).first()
        if not exists:
            return token
    raise HTTPException(status_code=500, detail="Failed to allocate asset token")


def _active_bytes(db: Session, **filters: Any) -> int:
    query = select(func.coalesce(func.sum(StaticAsset.size), 0)).where(
        StaticAsset.status == STATUS_ACTIVE
    )
    for column, value in filters.items():
        query = query.where(getattr(StaticAsset, column) == value)
    return int(db.exec(query).first() or 0)


@router.post("", response_model=StaticAssetRead)
async def upload_static_asset(
    tenant_id: str = Query(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StaticAssetRead:
    ensure_tenant(db, tenant_id)

    data = await file.read(STATIC_ASSET_MAX_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="上传的文件是空的")
    if len(data) > STATIC_ASSET_MAX_BYTES:
        limit_mb = STATIC_ASSET_MAX_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"单个文件不能超过 {limit_mb}MB")

    filename = safe_download_name(file.filename or "upload.bin")
    digest = sha256_of(data)

    # 同一个文件重复上传：复用已有记录，不新增行、不重复占磁盘。
    existing = db.exec(
        select(StaticAsset).where(
            StaticAsset.tenant_id == tenant_id,
            StaticAsset.owner_user_id == current_user.id,
            StaticAsset.sha256 == digest,
            StaticAsset.status == STATUS_ACTIVE,
        )
    ).first()
    if existing:
        return _to_read(existing)

    # 配额在写盘前校验：磁盘和服务同机，撑爆磁盘的代价远高于一次上传失败。
    user_used = _active_bytes(db, tenant_id=tenant_id, owner_user_id=current_user.id)
    if user_used + len(data) > STATIC_ASSET_USER_QUOTA_BYTES:
        quota_mb = STATIC_ASSET_USER_QUOTA_BYTES // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"个人空间已满（上限 {quota_mb}MB），请先删除旧文件")
    tenant_used = _active_bytes(db, tenant_id=tenant_id)
    if tenant_used + len(data) > STATIC_ASSET_TENANT_QUOTA_BYTES:
        raise HTTPException(status_code=413, detail="企业空间已满，请联系管理员清理")

    try:
        write_static_asset(tenant_id, digest, data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"文件保存失败：{exc}") from exc

    row = StaticAsset(
        tenant_id=tenant_id,
        owner_user_id=current_user.id,
        token=_new_token(db),
        filename=filename,
        content_type=content_type_for(filename),
        size=len(data),
        sha256=digest,
        status=STATUS_ACTIVE,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_read(row)


@router.get("", response_model=StaticAssetListRead)
def list_static_assets(
    tenant_id: str = Query(...),
    mine: bool = Query(False),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StaticAssetListRead:
    """列出资源。``mine=1`` 只看自己的；否则看租户内全部（供排查重复上传）。"""
    ensure_tenant(db, tenant_id)
    query = select(StaticAsset).where(
        StaticAsset.tenant_id == tenant_id,
        StaticAsset.status == STATUS_ACTIVE,
    )
    if mine:
        query = query.where(StaticAsset.owner_user_id == current_user.id)
    rows = db.exec(query.order_by(StaticAsset.created_at.desc())).all()  # type: ignore[attr-defined]
    return StaticAssetListRead(
        items=[_to_read(row) for row in rows],
        used_bytes=_active_bytes(db, tenant_id=tenant_id, owner_user_id=current_user.id),
        user_quota_bytes=STATIC_ASSET_USER_QUOTA_BYTES,
        max_file_bytes=STATIC_ASSET_MAX_BYTES,
    )


@router.delete("/{asset_id}", response_model=StaticAssetRead)
def delete_static_asset(
    asset_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StaticAssetRead:
    """软删：记录置 deleted（下载端点返 410），磁盘文件在没有其他引用时才真正删除。"""
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(StaticAsset).where(
            StaticAsset.id == asset_id,
            StaticAsset.tenant_id == tenant_id,
        )
    ).first()
    if row is None or row.status != STATUS_ACTIVE:
        raise HTTPException(status_code=404, detail="资源不存在")
    if row.owner_user_id != current_user.id and not _can_manage_others(current_user):
        raise HTTPException(status_code=403, detail="只能删除自己上传的资源")

    row.status = STATUS_DELETED
    row.deleted_at = utc_now()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()

    # 内容寻址下同一 sha 可能还有别的 active 记录引用，确认无人使用才删文件。
    shared = db.exec(
        select(StaticAsset.id).where(
            StaticAsset.tenant_id == tenant_id,
            StaticAsset.sha256 == row.sha256,
            StaticAsset.status == STATUS_ACTIVE,
        )
    ).first()
    if not shared:
        delete_static_asset_file(tenant_id, row.sha256)

    db.refresh(row)
    return _to_read(row)


def _can_manage_others(user: User) -> bool:
    """管理员可清理他人上传的资源（磁盘配额是租户共享的）。"""
    try:
        from app.security.permissions import is_admin_user
    except ImportError:  # pragma: no cover - 权限模块缺失时按最小权限处理
        return False
    return bool(is_admin_user(user))


@public_router.get("/f/{token}/{filename}")
def download_static_asset(
    token: str,
    filename: str,
    db: Session = Depends(get_session),
) -> Response:
    """公开下载（免登录）。

    ``filename`` 段只用于 Content-Disposition 的展示名，不参与定位 ——
    这样用户改名不会让已经发出去的链接失效。真实下载名以库里的 filename 为准。
    """
    row = db.exec(select(StaticAsset).where(StaticAsset.token == token)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    if row.status != STATUS_ACTIVE:
        # 410 而不是 404：能区分「被删了」和「链接本来就错」，排障成本差很多。
        raise HTTPException(status_code=410, detail="资源已被删除")

    data = read_static_asset(row.tenant_id, row.sha256)
    if data is None:
        raise HTTPException(status_code=404, detail="资源文件缺失")

    row.download_count = (row.download_count or 0) + 1
    db.add(row)
    db.commit()

    display_name = safe_download_name(row.filename)
    return Response(
        content=data,
        media_type=row.content_type or "application/octet-stream",
        headers={
            # 强制下载 + 禁止嗅探：上传的 html/svg 若在同源被解析执行，就能读走
            # localStorage 里的登录态。这两个头是这条链路的最后一道闸。
            "Content-Disposition": (
                f'attachment; filename="{_ascii_fallback(display_name)}"; '
                f"filename*=UTF-8''{quote(display_name, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(len(data)),
            # 内容不变（内容寻址），但允许被缓存 —— 下载大文件时省一半流量。
            "Cache-Control": "public, max-age=3600",
        },
    )


def _ascii_fallback(filename: str) -> str:
    """Content-Disposition 的兼容名字段：非 ASCII 会被部分客户端当乱码，这里退化成 download.bin 之外的兜底。"""
    ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
    return ascii_name or "download.bin"


__all__ = ["public_router", "router"]
