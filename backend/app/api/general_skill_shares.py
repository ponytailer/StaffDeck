"""通用技能分享链接。

技能广场里的技能可以生成一个公开链接：访客免登录查看技能描述并下载技能包。
站内口径与数字员工分享一致（token 唯一、可撤销、可过期、有访问计数），但
公开面收得很窄——只暴露描述性元信息与文件清单（不含文件内容），下载端点
仅对 ``status == "published"`` 的技能放行。
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.api.general_skills import (
    _general_skill_list_statement,
    _general_skill_package_archive,
    _get_general_skill_slim,
)
from app.db import get_session
from app.db.models import GeneralSkillShareLink, GeneralSkill, User, new_id, utc_now
from app.security.auth import get_current_user
from app.security.permissions import is_admin_user
from app.security.tenant import ensure_tenant

router = APIRouter(prefix="/api/enterprise/general-skills", tags=["enterprise:general-skill-shares"])
public_router = APIRouter(
    prefix="/api/public/general-skill-shares",
    tags=["general-skill-shares:public"],
)


class GeneralSkillShareRead(BaseModel):
    id: str
    token: str
    skill_id: str
    skill_slug: str
    expires_at: Optional[datetime] = None
    created_at: datetime


class GeneralSkillSharePublicInfo(BaseModel):
    """访客可见的技能分享元信息：只要名称/描述/文件数/总大小。

    文件明细不进公开响应（访客页面只做下载入口），也不读 skill_files_json
    大字段——文件清单在创建链接时缓存进链接行（files_json/total_bytes），
    公开读取只查 slim 列 + 链接行，20MB 包的公开页也不再有 MB 级 IO。
    """

    token: str
    slug: str
    name: str
    description: Optional[str] = None
    homepage: Optional[str] = None
    owner_name: Optional[str] = None
    file_count: int = 0
    total_bytes: int = 0
    expires_at: Optional[datetime] = None


def resolve_general_skill_share(
    db: Session, token: str, *, include_files: bool = True
) -> tuple[GeneralSkillShareLink, GeneralSkill]:
    """按 token 取分享链接 + 目标技能，并校验 (未撤销 / 未过期 / 技能仍已发布)。

    include_files=False 时用 slim 投影（不吃 MB 级 skill_files_json），供公开信息
    页这类只需要元信息的读路径用；下载路径仍拉完整行拿 skill_files_json。
    """
    row = db.exec(
        select(GeneralSkillShareLink).where(GeneralSkillShareLink.token == token)
    ).first()
    if not row or row.revoked_at is not None:
        raise HTTPException(status_code=404, detail="分享链接不存在或已失效")
    if row.expires_at is not None and row.expires_at <= utc_now():
        raise HTTPException(status_code=410, detail="分享链接已过期")
    if include_files:
        skill = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == row.tenant_id, GeneralSkill.id == row.skill_id
            )
        ).first()
    else:
        skill = db.exec(
            _general_skill_list_statement(
                row.tenant_id,
                include_files=False,
                extra_where=(GeneralSkill.id == row.skill_id,),
            )
        ).first()
    if not skill or skill.status != "published":
        raise HTTPException(status_code=404, detail="该技能已下架")
    return row, skill


def _require_shareable_skill_owner(
    db: Session, tenant_id: str, slug: str, current_user: User
) -> GeneralSkill:
    """能编辑这个技能（创建者或管理员）才能把它分享出去。"""
    row = _get_general_skill_slim(db, tenant_id, slug)
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    if not (is_admin_user(current_user) or metadata.get("owner_user_id") == current_user.id):
        raise HTTPException(status_code=403, detail="只有创建者或管理员可以分享技能")
    if row.status != "published":
        raise HTTPException(status_code=400, detail="技能已停用，无法分享")
    return row


@router.post("/{slug}/share", response_model=GeneralSkillShareRead)
def create_general_skill_share(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillShareRead:
    ensure_tenant(db, tenant_id)
    skill = _require_shareable_skill_owner(db, tenant_id, slug, current_user)
    # 幂等：同一技能已有未撤销链接时直接返回，避免重复生成一串新 token
    existing = db.exec(
        select(GeneralSkillShareLink)
        .where(
            GeneralSkillShareLink.tenant_id == tenant_id,
            GeneralSkillShareLink.skill_id == skill.id,
            GeneralSkillShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
        )
        .order_by(GeneralSkillShareLink.created_at.desc())
    ).first()
    if existing and (existing.expires_at is None or existing.expires_at > utc_now()):
        return GeneralSkillShareRead(
            id=existing.id,
            token=existing.token,
            skill_id=existing.skill_id,
            skill_slug=existing.skill_slug,
            expires_at=existing.expires_at,
            created_at=existing.created_at,
        )
    # 创建时把文件清单/总大小冗余进链接行：公开信息页只统计数量与体积，
    # 不再触碰 MB 级 skill_files_json（前端页只显示名称/说明/文件数/总大小）
    full_skill = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.id == skill.id
        )
    ).first()
    cached_files = [
        {"path": str(item.get("path") or ""), "size": item.get("size") if isinstance(item.get("size"), int) else None}
        for item in (full_skill.skill_files_json if full_skill and isinstance(full_skill.skill_files_json, list) else [])
        if isinstance(item, dict) and item.get("path")
    ]
    row = GeneralSkillShareLink(
        id=new_id("gsshare"),
        token=secrets.token_urlsafe(24),
        tenant_id=tenant_id,
        skill_id=skill.id,
        skill_slug=skill.slug,
        created_by=current_user.id,
        files_json=cached_files,
        total_bytes=sum(item["size"] or 0 for item in cached_files),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return GeneralSkillShareRead(
        id=row.id,
        token=row.token,
        skill_id=row.skill_id,
        skill_slug=row.skill_slug,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


@router.delete("/{slug}/share", response_model=dict)
def revoke_general_skill_share(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    ensure_tenant(db, tenant_id)
    skill = _require_shareable_skill_owner(db, tenant_id, slug, current_user)
    row = db.exec(
        select(GeneralSkillShareLink)
        .where(
            GeneralSkillShareLink.tenant_id == tenant_id,
            GeneralSkillShareLink.skill_id == skill.id,
            GeneralSkillShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="该技能没有有效的分享链接")
    row.revoked_at = utc_now()
    db.add(row)
    db.commit()
    return {"status": "revoked"}


@public_router.get("/{token}", response_model=GeneralSkillSharePublicInfo)
def read_general_skill_share(token: str, db: Session = Depends(get_session)) -> GeneralSkillSharePublicInfo:
    link, skill = resolve_general_skill_share(db, token, include_files=False)
    metadata = skill.metadata_json if isinstance(skill.metadata_json, dict) else {}
    # skill 列在这里是 slim 投影（resolve 提供的行来自轻查询），文件明细直接用链接行缓存
    cached_files = link.files_json if isinstance(link.files_json, list) else []
    return GeneralSkillSharePublicInfo(
        token=link.token,
        slug=skill.slug,
        name=skill.name,
        description=skill.description,
        homepage=skill.homepage,
        owner_name=str(metadata.get("owner_name") or "") or None,
        file_count=len(cached_files),
        total_bytes=link.total_bytes,
        expires_at=link.expires_at,
    )


@public_router.get("/{token}/package")
def download_shared_general_skill_package(
    token: str,
    db: Session = Depends(get_session),
):
    """公开下载技能包：免登录，但只对 published 技能、有效链接放行。"""
    link, skill = resolve_general_skill_share(db, token)
    archive = _general_skill_package_archive(skill)
    link.access_count += 1
    link.last_access_at = utc_now()
    db.add(link)
    db.commit()
    fallback_filename = f"{skill.slug}.zip"
    filename = f"{skill.name or skill.slug}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback_filename}\"; "
                f"filename*=UTF-8''{quote(filename, safe='')}"
            )
        },
    )
