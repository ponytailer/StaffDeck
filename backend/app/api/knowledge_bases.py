from __future__ import annotations

import math
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from app.capability_scope import normalize_capability_scope
from app.config import get_settings
from app.db import get_session
from app.db.bulk_delete import bulk_delete_matching
from app.agents.branching import (
    _binding_is_private,
    ensure_agent_private_knowledge_branch,
    ensure_knowledge_base_version,
    ensure_open_gallery_binding,
    get_agent,
    get_overall_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    is_personal_knowledge_base,
    knowledge_version_for_upload,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    personal_knowledge_base_rows,
    personal_knowledge_metadata,
    promote_knowledge_branch_to_overall,
    rollback_knowledge_branch,
    sync_knowledge_branch_from_overall,
    user_creator_metadata,
)
from app.db.models import (
    AgentKnowledgeBranch,
    AgentResourceBinding,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    User,
    utc_now,
)
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeConceptRead,
    KnowledgeConceptUpdateRequest,
    KnowledgeBaseRead,
    KnowledgeBaseRollbackRequest,
    KnowledgeBaseUpdateRequest,
)
from app.knowledge.route_cache import invalidate_knowledge_base
from app.knowledge.okf import (
    build_okf_for_document,
    export_okf_bundle,
    lint_okf_concepts,
    normalize_concept_id,
    parse_okf_markdown,
    persist_lint_issues,
    upsert_concepts,
)
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    ensure_personal_knowledge_owner,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/knowledge-bases",
    tags=["enterprise:knowledge-bases"],
    dependencies=[Depends(get_current_user)],
)

# 个人知识库对外开放的检索接口（账号级密钥）。路径里保留 ``{knowledge_base_id}``
# 占位符：前端会把它替换成用户当前选中的库，展示成可直接复制的示例。
KNOWLEDGE_OPEN_API_SCOPE = "knowledge:read"
KNOWLEDGE_OPEN_API_ENDPOINT_PATH = "/api/v1/knowledge-bases/{knowledge_base_id}:search"
# 集合检索：不传 knowledge_base_ids 即检索「我的全部个人知识库」
KNOWLEDGE_OPEN_API_COLLECTION_PATH = "/api/v1/knowledge:search"
KNOWLEDGE_OPEN_API_DOCS_PATH = "/api/v1/docs"


class KnowledgeApiAccessResponse(BaseModel):
    """「API 接入」指引弹窗要展示的接入信息（路径与对外基址都由后端给）。"""

    required_scope: str
    endpoint_path: str
    collection_endpoint_path: str
    docs_path: str
    # 来自 PUBLIC_BASE_URL；为空表示未配置，前端回退到当前 origin
    public_base_url: str


@router.get(
    "", response_model=None, dependencies=[Depends(require_agent_scope_viewer)]
)
def list_knowledge_bases(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    page: Annotated[int, Query(ge=1, le=200, description="页码，从 1 开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
    hide_empty_default: bool = True,
    # 「我的个人知识库」范围：不跟随任何数字员工，按创建者（owner_user_id）过滤。
    # 与 general-skills 的 ``mine=1`` 同一约定。数值型裸默认值方便测试直接调用。
    mine: bool = False,
    current_user: User | None = Depends(get_current_user),
) -> dict[str, object]:
    """列出知识库。

    服务端分页：返回 ``{items, total, page, page_size, total_pages, summary}``。
    ``total`` 以及 ``summary``（已上线 / 已下线 / 文档总数）均由独立的 SQL 聚合查询得出，
    不再依赖把全部知识库行拉进内存后再计数；列表本身也只回当前页，响应时间不再随知识库
    总量增长。

    ``mine=1`` 时返回当前用户的个人知识库（不绑定数字员工、不进广场）。
    """

    page = max(1, int(page))
    page_size = max(1, min(int(page_size), 200))

    ensure_tenant(db, tenant_id)
    if mine:
        if not isinstance(current_user, User):
            raise HTTPException(status_code=401, detail="Authentication required")
        ensure_current_user_tenant(tenant_id, current_user)
        rows = personal_knowledge_base_rows(
            db, tenant_id, current_user.id, include_inactive=True
        )
        return _knowledge_base_page_from_ids(
            db,
            tenant_id,
            [row.id for row in rows],
            page,
            page_size,
            hide_empty_default,
        )
    agent = get_agent(db, tenant_id, agent_id)

    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch)
            .where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
            .order_by(AgentKnowledgeBranch.updated_at.desc())
        ).all()
        # 先剔除两类「看不见」的库，再计数和切片，保证每页条数恒定：
        # 1) 指向已不存在（或跨租户）知识库的陈旧分支；
        # 2) 空默认知识库（与前端 isEmptyDefaultKnowledgeBase 同一判定）。
        branch_ids = [branch.knowledge_base_id for branch in branches]
        existing_ids = set(
            db.exec(
                select(KnowledgeBase.id).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.id.in_(branch_ids) if branch_ids else KnowledgeBase.id == "__none__",
                )
            ).all()
        )
        ordered_branches = [
            branch for branch in branches if branch.knowledge_base_id in existing_ids
        ]
        if hide_empty_default and ordered_branches:
            hidden_ids = _empty_default_knowledge_base_ids(
                db, tenant_id, [branch.knowledge_base_id for branch in ordered_branches]
            )
            if hidden_ids:
                ordered_branches = [
                    branch
                    for branch in ordered_branches
                    if branch.knowledge_base_id not in hidden_ids
                ]
        if not ordered_branches:
            return _empty_knowledge_base_envelope(page, page_size)
        ordered_ids = [branch.knowledge_base_id for branch in ordered_branches]
        total = _count_knowledge_bases(db, tenant_id, ordered_ids)
        summary = _knowledge_base_summary(db, tenant_id, ordered_ids)
        offset = (page - 1) * page_size
        if offset >= total:
            return _empty_knowledge_base_envelope(page, page_size, summary, total=total)
        page_branches = ordered_branches[offset : offset + page_size]
        page_kb_ids = [branch.knowledge_base_id for branch in page_branches]
        rows_by_id = {
            row.id: row
            for row in db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id,
                    KnowledgeBase.id.in_(page_kb_ids),
                )
            ).all()
        }
        versions: dict[str, KnowledgeBaseVersion] = {}
        for branch in page_branches:
            kb = rows_by_id.get(branch.knowledge_base_id)
            if kb:
                versions[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
        stats = _knowledge_base_stats(
            db, tenant_id, [version.id for version in versions.values()]
        )
        branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id)
        items = [
            knowledge_base_read(
                rows_by_id[branch.knowledge_base_id],
                stats.get(rows_by_id[branch.knowledge_base_id].id, {}),
                version_row=versions.get(rows_by_id[branch.knowledge_base_id].id),
                branch_meta=branch_meta.get(rows_by_id[branch.knowledge_base_id].id),
            )
            for branch in page_branches
            if branch.knowledge_base_id in rows_by_id
        ]
        return _knowledge_base_envelope(items, total, page, page_size, summary)

    # 开放广场 / 管理员视角：可见知识库 = 在整体智能体上有「未删除、非私有」绑定的知识库。
    visible_ids = _visible_open_gallery_knowledge_base_ids(db, tenant_id)
    if not visible_ids:
        return _empty_knowledge_base_envelope(page, page_size)
    ordered_ids = [
        row[0]
        for row in db.exec(
            select(KnowledgeBase.id, KnowledgeBase.updated_at)
            .where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(visible_ids),
            )
            .order_by(KnowledgeBase.updated_at.desc())
        ).all()
    ]
    return _knowledge_base_page_from_ids(
        db, tenant_id, ordered_ids, page, page_size, hide_empty_default
    )


def _knowledge_base_page_from_ids(
    db: Session,
    tenant_id: str,
    ordered_ids: list[str],
    page: int,
    page_size: int,
    hide_empty_default: bool = True,
) -> dict[str, object]:
    """把「已排好序的可见知识库 id」切成服务端分页（广场 / 个人库共用）。"""
    if hide_empty_default and ordered_ids:
        hidden_ids = _empty_default_knowledge_base_ids(db, tenant_id, ordered_ids)
        if hidden_ids:
            ordered_ids = [kb_id for kb_id in ordered_ids if kb_id not in hidden_ids]
    if not ordered_ids:
        return _empty_knowledge_base_envelope(page, page_size)
    total = _count_knowledge_bases(db, tenant_id, ordered_ids)
    summary = _knowledge_base_summary(db, tenant_id, ordered_ids)
    offset = (page - 1) * page_size
    if offset >= total:
        return _empty_knowledge_base_envelope(page, page_size, summary, total=total)
    page_ids = ordered_ids[offset : offset + page_size]
    rows = db.exec(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.id.in_(page_ids),
        )
    ).all()
    rows_by_id = {row.id: row for row in rows}
    versions = {
        kb_id: ensure_knowledge_base_version(db, rows_by_id[kb_id])
        for kb_id in page_ids
        if kb_id in rows_by_id
    }
    stats = _knowledge_base_stats(
        db, tenant_id, [version.id for version in versions.values()]
    )
    items = [
        knowledge_base_read(
            rows_by_id[kb_id],
            stats.get(kb_id, {}),
            version_row=versions.get(kb_id),
        )
        for kb_id in page_ids
        if kb_id in rows_by_id
    ]
    return _knowledge_base_envelope(items, total, page, page_size, summary)


def _empty_default_knowledge_base_ids(
    db: Session, tenant_id: str, kb_ids: list[str]
) -> set[str]:
    """与前端 isEmptyDefaultKnowledgeBase 对齐：返回应隐藏的「空默认知识库」 id。

    判定（无任何内容为前提，计数跨全部版本）：
    - metadata.created_from_document_upload 且无 source_document_id（上传创建但从未入库）；
    - 名字为「默认知识库」的空库。
    """
    if not kb_ids:
        return set()
    rows = db.exec(
        select(KnowledgeBase.id, KnowledgeBase.name, KnowledgeBase.metadata_json).where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.id.in_(kb_ids),
        )
    ).all()

    def _counts(model: type) -> dict[str, int]:
        return {
            knowledge_base_id: int(count or 0)
            for knowledge_base_id, count in db.exec(
                select(model.knowledge_base_id, func.count(model.id))
                .where(
                    model.tenant_id == tenant_id,
                    model.knowledge_base_id.in_(kb_ids),
                )
                .group_by(model.knowledge_base_id)
            ).all()
        }

    doc_counts = _counts(KnowledgeDocument)
    bucket_counts = _counts(KnowledgeBucket)
    chunk_counts = _counts(KnowledgeChunk)
    empty_ids: set[str] = set()
    for kb_id, name, metadata_json in rows:
        has_content = (
            doc_counts.get(kb_id, 0) > 0
            or bucket_counts.get(kb_id, 0) > 0
            or chunk_counts.get(kb_id, 0) > 0
        )
        if has_content:
            continue
        metadata = metadata_json or {}
        if metadata.get("created_from_document_upload") and not metadata.get(
            "source_document_id"
        ):
            empty_ids.add(kb_id)
        elif name == "默认知识库":
            empty_ids.add(kb_id)
    return empty_ids


def _visible_open_gallery_knowledge_base_ids(db: Session, tenant_id: str) -> list[str]:
    """整体智能体上「未删除、非私有」绑定的知识库 id（轻量 id-only 查询，不加载知识库行）。"""
    overall = get_overall_agent(db, tenant_id)
    if not overall:
        return []
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == overall.id,
            AgentResourceBinding.resource_type == "knowledge_base",
            AgentResourceBinding.status != "deleted",
        )
    ).all()
    return [
        binding.resource_id
        for binding in bindings
        if binding.resource_id and not _binding_is_private(binding)
    ]


def _count_knowledge_bases(db: Session, tenant_id: str, kb_ids: list[str]) -> int:
    if not kb_ids:
        return 0
    return int(
        db.exec(
            select(func.count())
            .select_from(KnowledgeBase)
            .where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(kb_ids),
            )
        ).one()
    )


def _knowledge_base_summary(
    db: Session, tenant_id: str, kb_ids: list[str]
) -> dict[str, int]:
    if not kb_ids:
        return {"active": 0, "archived": 0, "documents": 0}
    status_rows = db.exec(
        select(KnowledgeBase.status, func.count(KnowledgeBase.id))
        .where(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.id.in_(kb_ids),
        )
        .group_by(KnowledgeBase.status)
    ).all()
    status_counts = {status: int(count) for status, count in status_rows}
    documents = int(
        db.exec(
            select(func.count(KnowledgeDocument.id)).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.knowledge_base_id.in_(kb_ids),
            )
        ).one()
        or 0
    )
    return {
        "active": status_counts.get("active", 0) + status_counts.get("published", 0),
        "archived": status_counts.get("archived", 0),
        "documents": documents,
    }


def _empty_knowledge_base_envelope(
    page: int, page_size: int, summary: dict[str, int] | None = None, total: int = 0
) -> dict[str, object]:
    return {
        "items": [],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)) if total else 0,
        "summary": summary or {"active": 0, "archived": 0, "documents": 0},
    }


def _knowledge_base_envelope(
    items: list[KnowledgeBaseRead],
    total: int,
    page: int,
    page_size: int,
    summary: dict[str, int],
) -> dict[str, object]:
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total / page_size)),
        "summary": summary,
    }


@router.post("", response_model=KnowledgeBaseRead)
def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
    # mine=1：在「我的个人知识库」中新建（不绑定数字员工、不进广场）。
    mine: bool = False,
) -> KnowledgeBaseRead:
    ensure_tenant(db, request.tenant_id)
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
    existing = db.exec(
        select(KnowledgeBase).where(
            KnowledgeBase.tenant_id == request.tenant_id, KnowledgeBase.name == name
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists")
    agent = None
    if mine:
        if not isinstance(current_user, User):
            raise HTTPException(status_code=401, detail="Authentication required")
        ensure_current_user_tenant(request.tenant_id, current_user)
    else:
        agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
        if not (agent and not agent.is_overall):
            ensure_open_gallery_admin(request.tenant_id, current_user)
    creator_metadata = user_creator_metadata(current_user, request.metadata)
    row = KnowledgeBase(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        capability_scope=request.capability_scope,
        metadata_json=(
            personal_knowledge_metadata(creator_metadata) if mine else creator_metadata
        ),
        status="active",
    )
    db.add(row)
    db.flush()
    if mine:
        # 个人知识库刻意不建任何 AgentResourceBinding：
        # 广场列表、员工检索（visible_knowledge_base_versions）因此天然看不到它，
        # 只有显式「导入到数字员工」才会派生一份分支副本。
        pass
    elif agent and not agent.is_overall:
        mark_resource_private_for_agent(row, agent.id, creator_metadata)
        ensure_agent_private_knowledge_branch(
            db,
            request.tenant_id,
            agent.id,
            row,
            metadata_json=creator_metadata,
        )
    else:
        mark_resource_open_gallery(row, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            row.id,
            "active",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    return knowledge_base_read(row, {}, version_row=ensure_knowledge_base_version(db, row))


@router.get("/api-access", response_model=KnowledgeApiAccessResponse)
def knowledge_api_access(
    current_user: User = Depends(get_current_user),
) -> KnowledgeApiAccessResponse:
    """给「API 接入」窗口用的接入信息。

    必须声明在 ``@router.get("/{knowledge_base_id}")`` 之前：FastAPI 按声明顺序
    匹配路由，否则 ``/api-access`` 会被当成一个知识库 id。
    """

    settings = get_settings()
    return KnowledgeApiAccessResponse(
        required_scope=KNOWLEDGE_OPEN_API_SCOPE,
        endpoint_path=KNOWLEDGE_OPEN_API_ENDPOINT_PATH,
        collection_endpoint_path=KNOWLEDGE_OPEN_API_COLLECTION_PATH,
        docs_path=KNOWLEDGE_OPEN_API_DOCS_PATH,
        public_base_url=(settings.public_base_url or "").rstrip("/"),
    )


@router.get(
    "/{knowledge_base_id}",
    response_model=KnowledgeBaseRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    visible_version = _visible_knowledge_version(
        db, tenant_id, knowledge_base_id, agent_id, current_user
    )
    stats = _knowledge_base_stats(
        db,
        tenant_id,
        [visible_version.id],
    )
    branch_meta = _knowledge_branch_meta(db, tenant_id, agent_id).get(row.id)
    return knowledge_base_read(
        row,
        stats.get(row.id, {}),
        version_row=visible_version,
        branch_meta=branch_meta,
    )


@router.put("/{knowledge_base_id}", response_model=KnowledgeBaseRead)
def update_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeBaseRead:
    row = _get_knowledge_base(db, request.tenant_id, knowledge_base_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == request.tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(
                db, request.tenant_id, agent.id, knowledge_base_id
            )
        version_fields_changed = any(
            value is not None
            for value in (
                request.name,
                request.description,
                request.capability_scope,
                request.metadata,
            )
        )
        if version_fields_changed:
            version = knowledge_version_for_upload(
                db,
                request.tenant_id,
                knowledge_base_id,
                agent.id,
            )
        else:
            version = ensure_knowledge_base_version(db, row, branch.head_version)
        if request.name is not None:
            name = request.name.strip()
            if not name:
                raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
            version.name = name
        if request.description is not None:
            version.description = request.description
        if request.capability_scope is not None:
            version.capability_scope = request.capability_scope
        if request.metadata is not None:
            version.metadata_json = metadata_preserving_creator(
                version.metadata_json,
                request.metadata,
            )
        if request.status is not None:
            branch.status = "active" if request.status == "active" else "inactive"
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == request.tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "knowledge_base",
                    AgentResourceBinding.resource_id == knowledge_base_id,
                )
            ).first()
            if binding:
                binding.status = branch.status
                binding.updated_at = utc_now()
                db.add(binding)
        if (
            request.name is not None
            or request.description is not None
            or request.capability_scope is not None
            or request.metadata is not None
        ):
            branch.sync_state = "diverged"
        version.updated_at = utc_now()
        branch.updated_at = utc_now()
        db.add(version)
        db.add(branch)
        db.commit()
        db.refresh(row)
        stats = _knowledge_base_stats(db, request.tenant_id, [version.id]).get(row.id, {})
        return knowledge_base_read(
            row,
            stats,
            version_row=version,
            branch_meta={
                "base_version": branch.base_version,
                "head_version": branch.head_version,
                "sync_state": branch.sync_state,
                "status": branch.status,
            },
        )
    personal = is_personal_knowledge_base(row)
    if personal:
        ensure_personal_knowledge_owner(row, current_user)
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
    version = ensure_knowledge_base_version(db, row)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Knowledge base name cannot be empty")
        conflict = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == request.tenant_id,
                KnowledgeBase.name == name,
                KnowledgeBase.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Knowledge base name already exists")
        row.name = name
        version.name = name
    if request.description is not None:
        row.description = request.description
        version.description = request.description
    if request.capability_scope is not None:
        row.capability_scope = request.capability_scope
        version.capability_scope = request.capability_scope
    if request.status is not None:
        row.status = request.status
        version.status = request.status
    if request.metadata is not None:
        # 个人库的归属标记（visibility=personal / owner_user_id）不允许被编辑请求抹掉
        row_metadata = metadata_preserving_creator(row.metadata_json, request.metadata)
        version_metadata = metadata_preserving_creator(
            version.metadata_json,
            request.metadata,
        )
        if personal:
            row_metadata = personal_knowledge_metadata(row_metadata)
            version_metadata = personal_knowledge_metadata(version_metadata)
        row.metadata_json = row_metadata
        version.metadata_json = version_metadata
    row.updated_at = utc_now()
    version.updated_at = utc_now()
    db.add(row)
    db.add(version)
    db.flush()
    if request.status is not None and not personal:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "knowledge_base",
            row.id,
            "active" if request.status == "active" else "inactive",
        )
    db.commit()
    db.refresh(row)
    return knowledge_base_read(
        row,
        _knowledge_base_stats(db, request.tenant_id).get(row.id, {}),
        version_row=version,
    )


@router.get("/{knowledge_base_id}/versions", dependencies=[Depends(require_agent_scope_viewer)])
def list_knowledge_base_versions(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> list[dict[str, object]]:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id, current_user)
    agent = get_agent(db, tenant_id, agent_id)
    branch = None
    if agent and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
    rows = db.exec(
        select(KnowledgeBaseVersion)
        .where(
            KnowledgeBaseVersion.tenant_id == tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == row.id,
        )
        .order_by(KnowledgeBaseVersion.updated_at.desc())
    ).all()
    if agent and not agent.is_overall and branch:
        rows = [
            version
            for version in rows
            if version.version == branch.base_version
            or (version.metadata_json or {}).get("owner_agent_id") == agent.id
        ]
    else:
        rows = [
            version
            for version in rows
            if (version.metadata_json or {}).get("scope") != "agent_private"
        ]
    return [
        {
            "id": version.id,
            "version": version.version,
            "name": version.name,
            "description": version.description,
            "status": version.status,
            "capability_scope": normalize_capability_scope(version.capability_scope),
            "is_head": bool(branch and branch.head_version == version.version),
            "is_base": bool(branch and branch.base_version == version.version),
            "updated_at": version.updated_at.isoformat(),
            "created_at": version.created_at.isoformat(),
        }
        for version in rows
    ]


@router.get(
    "/{knowledge_base_id}/okf/concepts",
    response_model=list[KnowledgeConceptRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_okf_concepts(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    concept_type: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> list[KnowledgeConceptRead]:
    version = _visible_knowledge_version(
        db, tenant_id, knowledge_base_id, agent_id, current_user
    )
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    stmt = select(KnowledgeConcept).where(
        KnowledgeConcept.tenant_id == tenant_id,
        KnowledgeConcept.knowledge_base_id == knowledge_base_id,
        KnowledgeConcept.knowledge_base_version_id == version.id,
        KnowledgeConcept.status != "deleted",
    )
    if concept_type:
        stmt = stmt.where(KnowledgeConcept.concept_type == concept_type)
    rows = db.exec(stmt.order_by(KnowledgeConcept.concept_type, KnowledgeConcept.concept_id)).all()
    return [concept_read(row) for row in rows]


@router.get(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}",
    response_model=KnowledgeConceptRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> KnowledgeConceptRead:
    version = _visible_knowledge_version(
        db, tenant_id, knowledge_base_id, agent_id, current_user
    )
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    row = _get_concept(db, tenant_id, knowledge_base_id, version.id, concept_id)
    return concept_read(row)


@router.put(
    "/{knowledge_base_id}/okf/concepts/{concept_id:path}", response_model=KnowledgeConceptRead
)
def upsert_okf_concept(
    knowledge_base_id: str,
    concept_id: str,
    request: KnowledgeConceptUpdateRequest,
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> KnowledgeConceptRead:
    version = _writable_knowledge_version(
        db, request.tenant_id, knowledge_base_id, agent_id, current_user
    )
    document_id = _document_id_for_version(
        db, request.tenant_id, knowledge_base_id, version.id, request.document_id
    )
    parsed = parse_okf_markdown(concept_id, request.content_md)
    rows = upsert_concepts(
        db,
        request.tenant_id,
        knowledge_base_id,
        version.id,
        [
            {
                "concept_id": parsed.concept_id,
                "content_md": parsed.content_md,
                "document_id": document_id,
                "status": request.status,
            }
        ],
    )
    return concept_read(rows[0])


@router.get("/{knowledge_base_id}/okf/export", dependencies=[Depends(require_agent_scope_viewer)])
def export_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> Response:
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    version = _visible_knowledge_version(
        db, tenant_id, knowledge_base_id, agent_id, current_user
    )
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    rows = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == version.id,
            KnowledgeConcept.status == "active",
        )
        .order_by(KnowledgeConcept.concept_id)
    ).all()
    archive = export_okf_bundle(kb, version.id, rows)
    filename = f"{kb.name or knowledge_base_id}-okf-{version.version}.zip"
    fallback_filename = f"{knowledge_base_id}-okf-{version.version}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{fallback_filename}\"; filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.post("/{knowledge_base_id}/okf/lint", dependencies=[Depends(require_agent_scope_viewer)])
def lint_okf(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User | None = Depends(get_current_user),
) -> dict[str, object]:
    version = _visible_knowledge_version(
        db, tenant_id, knowledge_base_id, agent_id, current_user
    )
    _ensure_okf_concepts_for_version(db, tenant_id, knowledge_base_id, version.id)
    issues = lint_okf_concepts(db, tenant_id, knowledge_base_id, version.id)
    persist_lint_issues(db, tenant_id, knowledge_base_id, version.id, issues)
    return {"status": "ok", "issue_count": len(issues), "issues": issues}


@router.delete("/{knowledge_base_id}")
def delete_knowledge_base(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
            )
        ).first()
        if not branch:
            branch = sync_knowledge_branch_from_overall(db, tenant_id, agent.id, knowledge_base_id)
        branch.status = "deleted"
        branch.updated_at = utc_now()
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == row.id,
            )
        ).first()
        if not binding:
            binding = AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=agent.id,
                resource_type="knowledge_base",
                resource_id=row.id,
                status="deleted",
            )
        else:
            binding.status = "deleted"
            binding.updated_at = utc_now()
        db.add(branch)
        db.add(binding)
        db.commit()
        invalidate_knowledge_base(tenant_id, knowledge_base_id)
        return {"status": "hidden"}
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "knowledge_base", row):
            raise HTTPException(
                status_code=404, detail="Knowledge base not visible in open gallery"
            )
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "knowledge_base", row.id)
        db.commit()
        invalidate_knowledge_base(tenant_id, row.id)
        return {"status": "hidden"}
    if is_personal_knowledge_base(row):
        # 个人知识库只支持下线：外部（如 CMLG Office）可能以 knowledge_base_id 作
        # 稳定引用，硬删会让引用失效；属主想“清理”就下线（status=archived）。
        ensure_personal_knowledge_owner(row, current_user)
        raise HTTPException(
            status_code=400,
            detail="Personal knowledge bases can only be archived, not deleted",
        )
    ensure_open_gallery_admin(tenant_id, current_user)
    # 子表一律批量 DELETE：KnowledgeChunk.content / KnowledgeConcept.content_md /
    # KnowledgeDiscoverySuggestion.payload_json 都可能是 MB 级，一个大库有数千 chunk，
    # 原先「全量读进 ORM 再逐行 db.delete」纯粹是把这些字节搬进内存丢掉。
    for model in (
        KnowledgeDiscoverySuggestion,
        KnowledgeIngestJob,
        KnowledgeConcept,
        KnowledgeChunk,
        KnowledgeBucket,
        KnowledgeDocument,
        KnowledgeBaseVersion,
        AgentKnowledgeBranch,
    ):
        bulk_delete_matching(
            db, model, tenant_id=tenant_id, knowledge_base_id=row.id
        )
    bulk_delete_matching(
        db,
        AgentResourceBinding,
        tenant_id=tenant_id,
        resource_type="knowledge_base",
        resource_id=row.id,
    )
    db.delete(row)
    db.commit()
    invalidate_knowledge_base(tenant_id, row.id)
    return {"status": "deleted"}


@router.post("/{knowledge_base_id}/sync-from-overall")
def sync_knowledge_base_from_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    branch = sync_knowledge_branch_from_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {
        "status": "synced",
        "knowledge_base_id": knowledge_base_id,
        "head_version": branch.head_version,
    }


@router.post("/{knowledge_base_id}/promote-to-overall")
def promote_knowledge_base_to_overall(
    knowledge_base_id: str,
    tenant_id: str = Query(...),
    agent_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Overall agent does not have a branch to promote"
        )
    ensure_open_gallery_admin(tenant_id, current_user)
    version = promote_knowledge_branch_to_overall(db, tenant_id, agent_id, knowledge_base_id)
    db.commit()
    return {
        "status": "promoted",
        "knowledge_base_id": knowledge_base_id,
        "version": version.version,
    }


@router.post("/{knowledge_base_id}/rollback")
def rollback_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseRollbackRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    agent = ensure_agent_scope_manager(db, request.tenant_id, request.agent_id, current_user)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Use overall version management for trunk knowledge base"
        )
    branch = rollback_knowledge_branch(
        db, request.tenant_id, request.agent_id, knowledge_base_id, request.version
    )
    db.commit()
    return {
        "status": "rolled_back",
        "knowledge_base_id": knowledge_base_id,
        "head_version": branch.head_version,
    }


def knowledge_base_read(
    row: KnowledgeBase,
    stats: dict[str, int],
    version_row: KnowledgeBaseVersion | None = None,
    branch_meta: dict[str, str] | None = None,
) -> KnowledgeBaseRead:
    branch_status = (branch_meta or {}).get("status")
    if branch_status == "inactive":
        effective_status = "archived"
    elif branch_status == "active":
        effective_status = "active"
    elif branch_status:
        effective_status = branch_status
    else:
        effective_status = row.status
    return KnowledgeBaseRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=version_row.name if version_row else row.name,
        description=version_row.description if version_row else row.description,
        status=effective_status,
        capability_scope=normalize_capability_scope(
            version_row.capability_scope if version_row else row.capability_scope
        ),
        version=version_row.version if version_row else None,
        branch_sync_state=(branch_meta or {}).get("sync_state"),
        branch_base_version=(branch_meta or {}).get("base_version"),
        branch_head_version=(branch_meta or {}).get("head_version"),
        metadata=dict((version_row.metadata_json if version_row else row.metadata_json) or {}),
        document_count=int(stats.get("document_count", 0)),
        bucket_count=int(stats.get("bucket_count", 0)),
        chunk_count=int(stats.get("chunk_count", 0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def concept_read(row: KnowledgeConcept) -> KnowledgeConceptRead:
    return KnowledgeConceptRead(
        id=row.id,
        tenant_id=row.tenant_id,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_base_version_id=row.knowledge_base_version_id,
        document_id=row.document_id,
        concept_id=row.concept_id,
        concept_type=row.concept_type,
        title=row.title,
        description=row.description,
        content_md=row.content_md,
        frontmatter=row.frontmatter_json or {},
        links=row.links_json or [],
        citations=row.citations_json or [],
        source_refs=row.source_refs_json or [],
        status=row.status,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _visible_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    current_user: User | None = None,
) -> KnowledgeBaseVersion:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if is_personal_knowledge_base(row):
        # 个人知识库是 trunk 本体（没有分支），可见性只看属主。
        ensure_personal_knowledge_owner(row, current_user)
        return ensure_knowledge_base_version(db, row)
    versions = _management_knowledge_base_versions(db, tenant_id, agent_id)
    version = versions.get(knowledge_base_id)
    if not version:
        raise HTTPException(status_code=404, detail="Knowledge base version not visible")
    return version


def _writable_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    current_user: User,
) -> KnowledgeBaseVersion:
    row = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if is_personal_knowledge_base(row):
        ensure_personal_knowledge_owner(row, current_user)
        return ensure_knowledge_base_version(db, row)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        version = knowledge_version_for_upload(
            db,
            tenant_id,
            knowledge_base_id,
            agent.id,
            metadata_json=user_creator_metadata(current_user),
        )
        db.commit()
        return version
    ensure_open_gallery_admin(tenant_id, current_user)
    return _visible_knowledge_version(db, tenant_id, knowledge_base_id, agent_id)


def _ensure_okf_concepts_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> None:
    documents = db.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == version_id,
            KnowledgeDocument.status == "ready",
        )
    ).all()
    for document in documents:
        existing = db.exec(
            select(KnowledgeConcept.id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == version_id,
                KnowledgeConcept.document_id == document.id,
            )
        ).first()
        if existing:
            continue
        metadata = document.metadata_json or {}
        section_nodes = (
            metadata.get("section_tree") if isinstance(metadata.get("section_tree"), list) else []
        )
        buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == version_id,
                KnowledgeBucket.document_id == document.id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        if not section_nodes and not buckets:
            continue
        upsert_concepts(
            db,
            tenant_id,
            knowledge_base_id,
            version_id,
            build_okf_for_document(document, section_nodes, buckets),
        )


def _get_concept(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    concept_id: str,
) -> KnowledgeConcept:
    normalized = normalize_concept_id(concept_id)
    row = db.exec(
        select(KnowledgeConcept).where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeConcept.concept_id == normalized,
            KnowledgeConcept.status != "deleted",
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="OKF concept not found")
    return row


def _document_id_for_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    knowledge_base_version_id: str,
    document_id: str | None,
) -> str | None:
    if not document_id:
        return None
    current = db.get(KnowledgeDocument, document_id)
    if (
        current
        and current.tenant_id == tenant_id
        and current.knowledge_base_id == knowledge_base_id
        and current.knowledge_base_version_id == knowledge_base_version_id
    ):
        return current.id
    if (
        not current
        or current.tenant_id != tenant_id
        or current.knowledge_base_id != knowledge_base_id
    ):
        return document_id
    cloned = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == knowledge_base_version_id,
            KnowledgeDocument.filename == current.filename,
            KnowledgeDocument.file_type == current.file_type,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).first()
    return cloned.id if cloned else document_id


def _management_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
) -> dict[str, KnowledgeBaseVersion]:
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        branches = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).all()
        result: dict[str, KnowledgeBaseVersion] = {}
        for branch in branches:
            kb = db.get(KnowledgeBase, branch.knowledge_base_id)
            if not kb or kb.tenant_id != tenant_id:
                continue
            binding = db.exec(
                select(AgentResourceBinding).where(
                    AgentResourceBinding.tenant_id == tenant_id,
                    AgentResourceBinding.agent_id == agent.id,
                    AgentResourceBinding.resource_type == "knowledge_base",
                    AgentResourceBinding.resource_id == kb.id,
                )
            ).first()
            if not binding or not is_bound_resource_visible_for_agent(
                db, tenant_id, "knowledge_base", kb, binding
            ):
                continue
            result[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
        return result
    rows = db.exec(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant_id)).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "knowledge_base", row)]
    return {row.id: ensure_knowledge_base_version(db, row) for row in rows}


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    ensure_tenant(db, tenant_id)
    row = db.get(KnowledgeBase, knowledge_base_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return row


def _knowledge_base_stats(
    db: Session,
    tenant_id: str,
    version_ids: list[str] | None = None,
) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    doc_stmt = select(KnowledgeDocument.knowledge_base_id, func.count(KnowledgeDocument.id)).where(
        KnowledgeDocument.tenant_id == tenant_id
    )
    bucket_stmt = select(KnowledgeBucket.knowledge_base_id, func.count(KnowledgeBucket.id)).where(
        KnowledgeBucket.tenant_id == tenant_id
    )
    chunk_stmt = select(KnowledgeChunk.knowledge_base_id, func.count(KnowledgeChunk.id)).where(
        KnowledgeChunk.tenant_id == tenant_id
    )
    if version_ids is not None:
        doc_stmt = doc_stmt.where(KnowledgeDocument.knowledge_base_version_id.in_(version_ids))
        bucket_stmt = bucket_stmt.where(KnowledgeBucket.knowledge_base_version_id.in_(version_ids))
        chunk_stmt = chunk_stmt.where(KnowledgeChunk.knowledge_base_version_id.in_(version_ids))
    for knowledge_base_id, count in db.exec(
        doc_stmt.group_by(KnowledgeDocument.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["document_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        bucket_stmt.group_by(KnowledgeBucket.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["bucket_count"] = int(count or 0)
    for knowledge_base_id, count in db.exec(
        chunk_stmt.group_by(KnowledgeChunk.knowledge_base_id)
    ).all():
        stats.setdefault(knowledge_base_id, {})["chunk_count"] = int(count or 0)
    return stats


def _knowledge_branch_meta(
    db: Session, tenant_id: str, agent_id: str | None
) -> dict[str, dict[str, str]]:
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        return {}
    rows = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent.id,
            AgentKnowledgeBranch.status != "deleted",
        )
    ).all()
    return {
        row.knowledge_base_id: {
            "base_version": row.base_version,
            "head_version": row.head_version,
            "sync_state": row.sync_state,
            "status": row.status,
        }
        for row in rows
    }
