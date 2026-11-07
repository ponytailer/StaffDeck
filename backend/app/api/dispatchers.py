"""调度入口（Dispatcher）的企业侧 CRUD。

创建/更新都要求：**必须配置模型、成员 2~3 人、每人写好路由规则**。
这里不调 ``copy_overall_scope_to_agent`` —— 纯路由不该继承技能/知识库/工具。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.api.agents import (
    _agent_visible_to_user,
    _ensure_can_manage_agent,
    _ensure_request_tenant,
    _get_agent,
    _is_admin_user,
    _metadata_with_creator,
)
from app.db import get_session
from app.db.models import AgentProfile, DispatcherMember, ModelConfig, User, utc_now
from app.dispatch.router import DispatcherRouter
from app.dispatch.schema import (
    MAX_DISPATCH_MEMBERS,
    MIN_DISPATCH_MEMBERS,
    DispatchPreviewRequest,
    DispatchPreviewResponse,
    DispatcherCreateRequest,
    DispatcherMemberInput,
    DispatcherMemberRead,
    DispatcherRead,
    DispatcherUpdateRequest,
)
from app.dispatch.service import (
    DISPATCH_KIND,
    DispatchValidationError,
    bind_dispatcher_model,
    dispatch_config,
    dispatcher_model_config_id,
    list_dispatcher_members,
    list_dispatchers_for_member,
    member_has_own_model,
    replace_members,
    validate_member_inputs,
    write_dispatch_config,
)
from app.security.auth import get_current_user

router = APIRouter(prefix="/api/enterprise/dispatchers", tags=["enterprise:dispatchers"])


def _ensure_model(db: Session, tenant_id: str, model_config_id: str | None) -> str:
    """调度入口必须绑定一条可用模型。

    这是关掉免费额度链路的第一道闸：模型必填 → 子请求永远能带上
    model_config_id → ``_get_default_model`` 的免费回落与配额校验都不会触发。
    """
    if not model_config_id:
        raise HTTPException(status_code=400, detail="Dispatcher agents must configure a model")
    row = db.get(ModelConfig, model_config_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=400, detail="Model config not found")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="Model config is disabled")
    return row.id


def _read(
    db: Session, tenant_id: str, dispatcher: AgentProfile
) -> DispatcherRead:
    members = list_dispatcher_members(db, tenant_id, dispatcher.id)
    reads: list[DispatcherMemberRead] = []
    if members:
        agent_ids = [row.member_agent_id for row in members]
        agents = {
            row.id: row
            for row in db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id,
                    AgentProfile.id.in_(agent_ids),
                )
            ).all()
        }
        for row in members:
            agent = agents.get(row.member_agent_id)
            reads.append(
                DispatcherMemberRead(
                    member_agent_id=row.member_agent_id,
                    routing_rule=row.routing_rule,
                    example_queries=list(row.example_queries_json or []),
                    sort_order=row.sort_order,
                    status=row.status,
                    agent_name=agent.name if agent else "（已删除）",
                    agent_description=agent.description if agent else None,
                    agent_status=agent.status if agent else "deleted",
                    free_model_enabled=bool(agent.free_model_enabled) if agent else False,
                    has_own_model=member_has_own_model(db, tenant_id, row.member_agent_id),
                )
            )
    model_config_id = dispatcher_model_config_id(db, tenant_id, dispatcher.id)
    model = db.get(ModelConfig, model_config_id) if model_config_id else None
    return DispatcherRead(
        id=dispatcher.id,
        tenant_id=dispatcher.tenant_id,
        name=dispatcher.name,
        description=dispatcher.description,
        persona_prompt=dispatcher.persona_prompt,
        kind=dispatcher.kind or "standard",
        status=dispatcher.status,
        model_config_id=model_config_id,
        model_name=model.model if model else None,
        members=reads,
        config=dispatch_config(dispatcher),
        usage_count=dispatcher.usage_count,
        published_to_gallery=_published_to_gallery(dispatcher),
        created_at=dispatcher.created_at.isoformat() if dispatcher.created_at else None,
        updated_at=dispatcher.updated_at.isoformat() if dispatcher.updated_at else None,
    )


def _published_to_gallery(row: AgentProfile) -> bool:
    """与普通员工同一口径：metadata_json["published_to_gallery"] is True。"""
    return (row.metadata_json or {}).get("published_to_gallery") is True


def _ensure_members_visible(
    db: Session, user: User, members: list[DispatcherMemberInput]
) -> None:
    """成员必须是当前用户能看见的员工。

    ``validate_member_inputs`` 只校验同租户/存在/active，不看可见性——
    否则任何人都能把别人名下没公开的员工挂到自己入口上，等于绕开可见性
    去调用一个自己看不到的员工。
    """
    for item in members:
        agent = db.get(AgentProfile, item.member_agent_id)
        if agent is None:
            continue  # 存在性由 validate_member_inputs 统一报错
        if not _agent_visible_to_user(agent, user):
            raise HTTPException(status_code=403, detail="不能添加看不见的同事作为成员")


def _get_dispatcher(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    row = _get_agent(db, tenant_id, agent_id)
    if (row.kind or "standard") != DISPATCH_KIND:
        raise HTTPException(status_code=404, detail="Dispatcher not found")
    return row


def _preview_dispatcher_stub(request: DispatchPreviewRequest) -> AgentProfile:
    """试跑用的临时入口对象（**不入库**）。

    ``DispatcherRouter.decide`` 的签名要求一个 ``AgentProfile``，但它只用来
    打日志/兜底话术，不读 id，所以这里给个游离对象即可，不必伪造 id。
    """
    return AgentProfile(
        tenant_id=request.tenant_id,
        name="（试跑）",
        kind=DISPATCH_KIND,
        status="active",
    )


def _resolve_preview_model(
    db: Session, tenant_id: str, model_config_id: str | None, user: User
):
    """试跑用模型：配了就用配的，没配就用租户默认。

    这里**允许**回落（生产路径的 dispatcher 必须配模型），因为配置页在第一段
    还没选模型时用户也可能想点一下试跑；没有可用模型就返回 None，
    路由会走 fallback 而不是 500。
    """
    if model_config_id:
        row = db.get(ModelConfig, model_config_id)
        if row is not None and row.tenant_id == tenant_id and row.enabled:
            from app.llm.model_config_resolver import resolve_model_config_for_runtime

            try:
                return resolve_model_config_for_runtime(db, tenant_id, row.id)
            except Exception:  # 未验证/指纹不符等 → 让路由走兜底，别在这儿 500
                return None
    from app.agents.branching import model_for_agent

    try:
        return model_for_agent(db, tenant_id, None, "default", user_id=user.id)  # type: ignore[arg-type]
    except Exception:
        return None


@router.post("", response_model=DispatcherRead)
def create_dispatcher(
    request: DispatcherCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatcherRead:
    _ensure_request_tenant(request.tenant_id, current_user)
    name = (request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="调度入口名称不能为空")
    existing = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == request.tenant_id, AgentProfile.name == name
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="同名员工已存在")

    model_config_id = _ensure_model(db, request.tenant_id, request.model_config_id)
    _ensure_members_visible(db, current_user, request.members)

    # 先校验成员再落库：配置不合法时不留半成品入口
    try:
        # 创建时还没有 id，传空串即可（空串不会命中「不能绑自己」的判定）
        validated = validate_member_inputs(db, request.tenant_id, "", request.members)
    except DispatchValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = AgentProfile(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        persona_prompt=request.persona_prompt,
        kind=DISPATCH_KIND,
        status="active",
        # 必须复用 agents.py 的 creator 元数据写法：_ensure_can_manage_agent
        # 靠 metadata_json["owner_user_id"] 判定所有权，漏了它非管理员
        # 连自己建的调度入口都改不了。
        metadata_json=_metadata_with_creator({}, current_user),
    )
    db.add(row)
    db.flush()
    # 落库时再走一遍（这一次带上真实 id，防「绑自己」的兜底也生效）
    try:
        replace_members(db, row, validated)
    except DispatchValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    bind_dispatcher_model(db, row, model_config_id)
    write_dispatch_config(row, request.config)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(db, request.tenant_id, row)


@router.get("", response_model=list[DispatcherRead])
def list_dispatchers(
    tenant_id: str = Query(...),
    gallery_only: bool = Query(
        False,
        description="只返回已发布到开放广场「员工小队」的（广场列表用，谁也不该看到别人未发布的入口）",
    ),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[DispatcherRead]:
    _ensure_request_tenant(tenant_id, current_user)
    statement = select(AgentProfile).where(
        AgentProfile.tenant_id == tenant_id,
        AgentProfile.kind == DISPATCH_KIND,
    )
    if gallery_only:
        statement = statement.where(AgentProfile.status == "active")
    rows = db.exec(statement.order_by(AgentProfile.created_at.desc())).all()
    if gallery_only:
        # 广场口径：只认 published_to_gallery，且不再按归属过滤 —— 广场本来就是
        # 「别人的东西我看看能不能用」。
        return [
            _read(db, tenant_id, row)
            for row in rows
            if _published_to_gallery(row)
        ]
    # 可见性口径与 /api/enterprise/agents 保持一致：管理员看全部，
    # 普通用户只看自己建的（调度入口是「会路由的同事」，混进别人的入口没意义）。
    if not _is_admin_user(current_user):
        rows = [row for row in rows if _agent_visible_to_user(row, current_user)]
    return [_read(db, tenant_id, row) for row in rows]


@router.get("/{agent_id}", response_model=DispatcherRead)
def get_dispatcher(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatcherRead:
    row = _get_dispatcher(db, tenant_id, agent_id)
    # 详情走可见性判定而不是管理权判定：同一租户的同事要能看到入口才能去聊它，
    # 写操作才要求创建者/管理员。
    if not _agent_visible_to_user(row, current_user):
        raise HTTPException(status_code=403, detail="Only the creator or administrator can view this dispatcher")
    return _read(db, tenant_id, row)


@router.put("/{agent_id}", response_model=DispatcherRead)
def update_dispatcher(
    agent_id: str,
    request: DispatcherUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatcherRead:
    row = _get_dispatcher(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)

    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="调度入口名称不能为空")
        if name != row.name:
            clash = db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == request.tenant_id,
                    AgentProfile.name == name,
                    AgentProfile.id != row.id,
                )
            ).first()
            if clash is not None:
                raise HTTPException(status_code=409, detail="同名员工已存在")
            row.name = name
    if request.description is not None:
        row.description = request.description
    if request.persona_prompt is not None:
        row.persona_prompt = request.persona_prompt

    # 模型：不传 = 沿用；显式传 null 由 pydantic 与「不传」无法区分，
    # 所以这里按「传了就校验」处理——空串视为摘掉模型，直接拒绝。
    if request.model_config_id is not None:
        model_config_id = _ensure_model(db, request.tenant_id, request.model_config_id)
        bind_dispatcher_model(db, row, model_config_id)

    if request.members is not None:
        _ensure_members_visible(db, current_user, request.members)
        try:
            replace_members(db, row, request.members)
        except DispatchValidationError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.config is not None:
        write_dispatch_config(row, request.config)

    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(db, request.tenant_id, row)


@router.post("/{agent_id}/gallery:publish", response_model=DispatcherRead)
def publish_dispatcher_to_gallery(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatcherRead:
    """发布到开放广场「员工小队」。

    只改 metadata，**不动成员、模型、路由规则**——上广场只是让别人「看得见 + 能去聊」，
    不是复制一份配置。与普通员工的 `published_to_gallery` 同一套字段，口径统一。
    """
    row = _get_dispatcher(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    # 广场卡片要展示「转给谁」，成员不足 2 位时上广场等于把一个不会路由的入口推给别人。
    # 这里不拦（用户可能先发布再补成员），但要保证成员行本身是健康的：
    # 成员全部被删/归档的入口一律拒绝发布。
    members = list_dispatcher_members(db, tenant_id, row.id)
    if not members:
        raise HTTPException(
            status_code=400,
            detail="该多智能体还没有成员，先添加同事并写好路由规则再发布",
        )
    metadata = dict(row.metadata_json or {})
    if metadata.get("published_to_gallery") is True:
        return _read(db, tenant_id, row)
    now = utc_now()
    metadata["published_to_gallery"] = True
    metadata["gallery_published_at"] = now.isoformat()
    metadata["gallery_published_by"] = current_user.username
    metadata.pop("gallery_unpublished_at", None)
    metadata.pop("gallery_unpublished_by", None)
    row.metadata_json = metadata
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(db, tenant_id, row)


@router.post("/{agent_id}/gallery:unpublish", response_model=DispatcherRead)
def unpublish_dispatcher_from_gallery(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatcherRead:
    """从广场下架：只摘发布标记，入口本体、成员、会话全部保留。"""
    row = _get_dispatcher(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    metadata = dict(row.metadata_json or {})
    if metadata.get("published_to_gallery") is not True:
        return _read(db, tenant_id, row)
    now = utc_now()
    metadata["published_to_gallery"] = False
    metadata["gallery_unpublished_at"] = now.isoformat()
    metadata["gallery_unpublished_by"] = current_user.username
    metadata.pop("gallery_published_at", None)
    metadata.pop("gallery_published_by", None)
    row.metadata_json = metadata
    row.updated_at = now
    db.add(row)
    db.commit()
    db.refresh(row)
    return _read(db, tenant_id, row)


@router.delete("/{agent_id}")
def delete_dispatcher(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    row = _get_dispatcher(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    # 删入口只需清它自己的成员绑定；会话清理与员工删除共用同一套逻辑
    from app.api.agents import delete_agent

    delete_agent(agent_id=agent_id, tenant_id=tenant_id, db=db, current_user=current_user)
    return {"status": "deleted"}


@router.post("/preview", response_model=DispatchPreviewResponse)
def preview_route(
    request: DispatchPreviewRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> DispatchPreviewResponse:
    """配置页「试跑规则」：用草稿成员跑一次真实路由，**不落库、不建会话**。

    为什么必须有这个端点：配置页的三段式里，规则写得好不好只有真跑一次才知道；
    让用户「先保存再去对话试」等于把配置错误推迟到业务现场。
    """
    _ensure_request_tenant(request.tenant_id, current_user)
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="请输入要试跑的问题")

    # 成员数量下限在保存时强制，试跑时更该拦：只有 1 个成员时路由比分永远 100%，
    # 会给出「规则配得挺好」的假信号。
    if len(request.members) < MIN_DISPATCH_MEMBERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"至少要添加 {MIN_DISPATCH_MEMBERS} 个同事才能试跑"
                " —— 只有一位时不需要调度，直接找他就行"
            ),
        )
    if len(request.members) > MAX_DISPATCH_MEMBERS:
        raise HTTPException(
            status_code=400, detail=f"最多只能添加 {MAX_DISPATCH_MEMBERS} 个同事"
        )

    # 组装草稿成员：真正落库的 DispatcherMember 行 + 真实 AgentProfile，
    # 让 DispatcherRouter 完全走生产路径（同样的 prompt、同样的归一化、同样的兜底）。
    draft_rows: list[tuple[DispatcherMember, AgentProfile]] = []
    seen: set[str] = set()
    for index, item in enumerate(request.members):
        member_id = str(item.member_agent_id or "").strip()
        if not member_id:
            raise HTTPException(status_code=400, detail="成员不能为空")
        if member_id in seen:
            raise HTTPException(status_code=400, detail="同一个同事只能添加一次")
        seen.add(member_id)
        if request.dispatcher_id and member_id == request.dispatcher_id:
            raise HTTPException(status_code=400, detail="调度入口不能把自己设为成员")
        agent = db.get(AgentProfile, member_id)
        if agent is None or agent.tenant_id != request.tenant_id:
            raise HTTPException(status_code=400, detail="选中的同事不存在")
        if (agent.kind or "standard") == DISPATCH_KIND:
            raise HTTPException(
                status_code=400, detail="不能把另一个调度入口设为成员（不支持嵌套调度）"
            )
        if not _agent_visible_to_user(agent, current_user):
            raise HTTPException(status_code=403, detail="不能添加看不见的同事作为成员")
        # 未落库的临时行：只拿来喂 prompt，不会被 flush
        draft_rows.append(
            (
                DispatcherMember(
                    tenant_id=request.tenant_id,
                    dispatcher_agent_id=request.dispatcher_id or "draft",
                    member_agent_id=member_id,
                    routing_rule=str(item.routing_rule or "").strip(),
                    example_queries_json=[
                        str(q).strip() for q in (item.example_queries or []) if str(q).strip()
                    ][:12],
                    sort_order=item.sort_order if item.sort_order else index,
                    status="active",
                ),
                agent,
            )
        )

    model = _resolve_preview_model(db, request.tenant_id, request.model_config_id, current_user)
    decision = DispatcherRouter().decide(
        db=db,
        dispatcher=_preview_dispatcher_stub(request),
        members=draft_rows,
        message=message,
        config=request.config,
        parent_session=None,
        model_config=model,
    )

    by_id = {agent.id: agent for _row, agent in draft_rows}
    hit = by_id.get(decision.member_agent_id or "")
    return DispatchPreviewResponse(
        decision=decision,
        member_name=hit.name if hit else None,
        member_description=hit.description if hit else None,
        candidates=[
            {"agent_id": agent.id, "name": agent.name, "description": agent.description or ""}
            for _row, agent in draft_rows
        ],
    )


@router.get("/{agent_id}/referenced-by")
def referenced_by(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """这个员工被哪些调度入口引用（删员工前提示用）。"""
    _ensure_request_tenant(tenant_id, current_user)
    ids = list_dispatchers_for_member(db, tenant_id, agent_id)
    rows = (
        db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id, AgentProfile.id.in_(ids)
            )
        ).all()
        if ids
        else []
    )
    return {
        "count": len(ids),
        "dispatchers": [{"id": row.id, "name": row.name} for row in rows],
    }