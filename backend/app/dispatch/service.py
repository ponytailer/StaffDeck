"""调度入口（Dispatcher）的服务层：成员 CRUD、上下文拼装、模型解析。

三条不可退让的口径（用户 2026-10-30 拍板，见 docs/agent-dispatcher-plan.md）：
1. 路由规则**在创建/编辑时**配好，对话时用户不参与选择。
2. 上下文职责切分：**父会话只维护「转接记录」+ 答案快照（仅展示）；子会话维护自己的 QA。**
3. **模型与配额全部落在调度入口上**：子请求显式带 model_config_id，
   免费额度链路（chat.py 的 `not request.model_config_id and request.agent_id`）永不触发。
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Iterable

from sqlmodel import Session, select

from app.db.bulk_delete import bulk_delete_matching
from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    ChatSession,
    DispatcherMember,
    ModelConfig,
    utc_now,
)
from app.dispatch.schema import (
    DEFAULT_CLARIFY_TEXT,
    DEFAULT_FALLBACK_REPLY,
    MAX_DISPATCH_MEMBERS,
    MIN_DISPATCH_MEMBERS,
    DispatchConfig,
    DispatcherMemberInput,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.llm.model_config_resolver import ResolvedModelConfig

# 子会话的 channel 常量以 origin.py 为单一事实源（那里负责「侧边栏隐藏」）
from app.session.origin import DISPATCH_CHILD_CHANNEL  # noqa: E402

DISPATCH_KIND = "dispatcher"
STANDARD_KIND = "standard"
DISPATCH_LOG_LIMIT = 10

# 会话 context_state_json 里的键名（都存在既有 JSON 字段，不新建表）
DISPATCH_LOG_KEY = "dispatch_log"
DISPATCH_PARENT_KEY = "dispatch_parent_session_id"
ACTIVE_DISPATCH_KEY = "active_dispatch"
DISPATCH_CANCEL_KEY = "dispatch_cancel_requested"


class DispatchValidationError(ValueError):
    """成员配置不合法。路由层译成 400，不要透技术栈信息。"""


# ────────────────────────────────────────────────────────────
# 基础判定
# ────────────────────────────────────────────────────────────


def is_dispatcher(agent: AgentProfile | None) -> bool:
    return agent is not None and (agent.kind or STANDARD_KIND) == DISPATCH_KIND


def dispatch_config(dispatcher: AgentProfile) -> DispatchConfig:
    """从 metadata_json 读兜底配置，缺省补默认值（老数据/手改脏数据都不炸）。"""
    raw = (dispatcher.metadata_json or {}).get("dispatch_config")
    if not isinstance(raw, dict):
        return DispatchConfig()
    try:
        return DispatchConfig.model_validate(raw)
    except Exception:
        return DispatchConfig()


def write_dispatch_config(dispatcher: AgentProfile, config: DispatchConfig) -> None:
    metadata = dict(dispatcher.metadata_json or {})
    metadata["dispatch_config"] = config.model_dump(mode="json")
    dispatcher.metadata_json = metadata
    dispatcher.updated_at = utc_now()


# 小队头像存在 metadata 里（与员工的 avatar_preset 同一套命名），不新增列：
# 旧数据没有这个 key 时返回 None，前端回落默认图形，不需要数据迁移。
DISPATCHER_AVATAR_METADATA_KEY = "avatar_preset"


def dispatcher_avatar_preset(dispatcher: AgentProfile) -> str | None:
    """读小队头像预设；缺省/脏数据一律返回 None（前端兜底默认图形）。"""
    raw = (dispatcher.metadata_json or {}).get(DISPATCHER_AVATAR_METADATA_KEY)
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or None
    return None


def write_dispatcher_avatar_preset(dispatcher: AgentProfile, preset: str | None) -> None:
    """写小队头像；传空值表示清掉、回落到默认图形。"""
    metadata = dict(dispatcher.metadata_json or {})
    cleaned = (preset or "").strip()
    if cleaned:
        metadata[DISPATCHER_AVATAR_METADATA_KEY] = cleaned
    else:
        metadata.pop(DISPATCHER_AVATAR_METADATA_KEY, None)
    dispatcher.metadata_json = metadata
    dispatcher.updated_at = utc_now()


# ────────────────────────────────────────────────────────────
# 成员读取
# ────────────────────────────────────────────────────────────


def list_dispatcher_members(
    db: Session,
    tenant_id: str,
    dispatcher_agent_id: str,
    *,
    active_only: bool = True,
) -> list[DispatcherMember]:
    statement = select(DispatcherMember).where(
        DispatcherMember.tenant_id == tenant_id,
        DispatcherMember.dispatcher_agent_id == dispatcher_agent_id,
    )
    if active_only:
        statement = statement.where(DispatcherMember.status == "active")
    statement = statement.order_by(DispatcherMember.sort_order.asc(), DispatcherMember.created_at.asc())
    return list(db.exec(statement).all())


def list_dispatchers_for_member(db: Session, tenant_id: str, member_agent_id: str) -> list[str]:
    """反查：这个员工被哪些调度入口引用（删员工时要提示用户，编辑页要显示）。"""
    rows = db.exec(
        select(DispatcherMember.dispatcher_agent_id).where(
            DispatcherMember.tenant_id == tenant_id,
            DispatcherMember.member_agent_id == member_agent_id,
            DispatcherMember.status == "active",
        )
    ).all()
    return [str(item) for item in rows if item]


def load_active_members_with_agents(
    db: Session, tenant_id: str, dispatcher_agent_id: str
) -> list[tuple[DispatcherMember, AgentProfile]]:
    """一次查询取回成员行 + 员工档案，按 sort_order 排好。

    成员被归档/删除时**不静默丢**：这里只返回仍存活的，
    调用方据此判断「有效成员数」，全失效就该走 fallback。
    """
    members = list_dispatcher_members(db, tenant_id, dispatcher_agent_id)
    if not members:
        return []
    agent_ids = [m.member_agent_id for m in members]
    agents = {
        row.id: row
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id.in_(agent_ids),
            )
        ).all()
    }
    result: list[tuple[DispatcherMember, AgentProfile]] = []
    for member in members:
        agent = agents.get(member.member_agent_id)
        if agent is None or agent.status != "active":
            continue
        # kind=dispatcher 的成员是脏数据（禁环），路由时直接剔除
        if is_dispatcher(agent):
            continue
        result.append((member, agent))
    return result


# ────────────────────────────────────────────────────────────
# 成员写入（整体替换）
# ────────────────────────────────────────────────────────────


def validate_member_inputs(
    db: Session,
    tenant_id: str,
    dispatcher_agent_id: str,
    items: Iterable[DispatcherMemberInput],
    *,
    enforce_min: bool = True,
) -> list[DispatcherMemberInput]:
    """校验成员配置。越界一律抛 DispatchValidationError（路由层译 400）。

    规则：
    - 数量在 [MIN_DISPATCH_MEMBERS, MAX_DISPATCH_MEMBERS]
    - 不能绑自己；不能绑另一个 dispatcher（禁环，天然封死嵌套）
    - 必须同租户、active、存在
    - 不能重复绑同一人

    ``enforce_min=False``：成员被删除后重新编辑（成员数可能已跌到 0/1），
    允许先保存其它改动，但路由时必然走 fallback。只在
    ``PUT`` 的「成员数比库里还少且没有新增」场景不需要 —— 实际上 PUT 也强制，
    保持口径一致，这个开关留给内部修复脚本用。
    """
    normalized: list[DispatcherMemberInput] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        member_id = str(item.member_agent_id or "").strip()
        if not member_id:
            raise DispatchValidationError("成员不能为空")
        if member_id == dispatcher_agent_id:
            raise DispatchValidationError("调度入口不能把自己设为成员")
        if member_id in seen:
            raise DispatchValidationError("同一个同事只能添加一次")
        seen.add(member_id)
        agent = db.get(AgentProfile, member_id)
        if agent is None or agent.tenant_id != tenant_id or agent.status != "active":
            raise DispatchValidationError("选中的同事不存在或已停用")
        if is_dispatcher(agent):
            raise DispatchValidationError("不能把另一个调度入口设为成员（不支持嵌套调度）")
        normalized.append(
            DispatcherMemberInput(
                member_agent_id=member_id,
                routing_rule=str(item.routing_rule or "").strip(),
                example_queries=[
                    str(q).strip() for q in (item.example_queries or []) if str(q).strip()
                ][:12],
                sort_order=item.sort_order if item.sort_order else index,
            )
        )
    if len(normalized) > MAX_DISPATCH_MEMBERS:
        raise DispatchValidationError(f"最多只能添加 {MAX_DISPATCH_MEMBERS} 个同事")
    if enforce_min and len(normalized) < MIN_DISPATCH_MEMBERS:
        raise DispatchValidationError(
            f"至少要有 {MIN_DISPATCH_MEMBERS} 个同事才需要调度入口；"
            "只有一位同事时，直接找他聊就行"
        )
    return normalized


def replace_members(
    db: Session,
    dispatcher: AgentProfile,
    items: list[DispatcherMemberInput],
) -> list[DispatcherMember]:
    """整体替换成员（编辑页一直是「全量提交」语义，避免增量 diff 的边界 bug）。

    先校验再删（校验失败不破坏既有配置），删除走批量删除。
    """
    normalized = validate_member_inputs(
        db, dispatcher.tenant_id, dispatcher.id, items
    )
    bulk_delete_matching(
        db, DispatcherMember, dispatcher_agent_id=dispatcher.id
    )
    rows: list[DispatcherMember] = []
    for item in normalized:
        row = DispatcherMember(
            tenant_id=dispatcher.tenant_id,
            dispatcher_agent_id=dispatcher.id,
            member_agent_id=item.member_agent_id,
            routing_rule=item.routing_rule,
            example_queries_json=list(item.example_queries),
            sort_order=item.sort_order,
            status="active",
        )
        db.add(row)
        rows.append(row)
    dispatcher.updated_at = utc_now()
    db.flush()
    return rows


# ────────────────────────────────────────────────────────────
# 模型解析（关掉免费额度的核心）
# ────────────────────────────────────────────────────────────


def dispatcher_model_config_id(db: Session, tenant_id: str, dispatcher_agent_id: str) -> str | None:
    """调度入口自己绑定的模型 id。"""
    binding = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == dispatcher_agent_id,
            AgentModelBinding.role == "default",
        )
    ).first()
    return binding.model_config_id if binding else None


def bind_dispatcher_model(
    db: Session, dispatcher: AgentProfile, model_config_id: str
) -> None:
    """把模型写进 AgentModelBinding（同一员工同一角色只留一条）。"""
    existing = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == dispatcher.tenant_id,
            AgentModelBinding.agent_id == dispatcher.id,
            AgentModelBinding.role == "default",
        )
    ).first()
    if existing is not None:
        existing.model_config_id = model_config_id
        existing.updated_at = utc_now()
        db.add(existing)
    else:
        db.add(
            AgentModelBinding(
                tenant_id=dispatcher.tenant_id,
                agent_id=dispatcher.id,
                role="default",
                model_config_id=model_config_id,
            )
        )
    db.flush()


def resolve_dispatcher_model(
    db: Session, tenant_id: str, dispatcher: AgentProfile, user_id: str | None
) -> "ResolvedModelConfig":
    """解析调度入口本轮的模型。

    调度入口**必须**配了模型才能创建（API 层强校验），所以这里正常都能取到。
    取不到（模型被删/停用）才回落到租户内可用模型——仍然不碰免费链路。
    """
    from app.agents.branching import model_for_agent
    from app.llm.model_config_resolver import resolve_model_config_for_runtime

    config_id = dispatcher_model_config_id(db, tenant_id, dispatcher.id)
    if config_id:
        row = db.get(ModelConfig, config_id)
        if row is not None and row.tenant_id == tenant_id and row.enabled:
            return resolve_model_config_for_runtime(db, tenant_id, row.id)
    # 兜底：租户内可用模型（model_for_agent 不含免费回落）
    resolved = model_for_agent(db, tenant_id, dispatcher.id, "default", user_id=user_id)
    if resolved is None:
        raise DispatchValidationError("调度入口没有可用的模型，请先在配置里选择模型")
    return resolved


def resolve_dispatch_member_model(
    db: Session,
    tenant_id: str,
    member: AgentProfile,
    dispatcher_model: "ResolvedModelConfig",
    user_id: str | None = None,
) -> "ResolvedModelConfig":
    """子 turn 的模型：成员自己配了就用成员的（谁配谁付费），否则用调度入口的。

    **不能调 ``model_for_agent``**：那个函数签名收 agent_id 但内部 ``_ = agent_id``，
    实际按「当前用户的 is_default 模型 → 租户里任意一个 is_default 模型」解析，
    拿到的根本不是成员绑定的那条。这里必须直读 ``AgentModelBinding``。

    也**绝不调** ``_get_default_model`` / ``resolve_turn_default_model`` ——
    那条链带免费模型回落（facade_runtime.py 的 allow_free_model 段），
    正是「免费额度链路一律不参与」要避开的东西。
    """
    from app.llm.model_config_resolver import resolve_model_config_for_runtime

    bound_id = dispatcher_model_config_id(db, tenant_id, member.id)
    if bound_id:
        row = db.get(ModelConfig, bound_id)
        # 停用/删除的绑定按「没配」处理：回落到调度入口的模型，不要让子 turn 直接失败
        if row is not None and row.tenant_id == tenant_id and row.enabled:
            return resolve_model_config_for_runtime(db, tenant_id, row.id)
    return dispatcher_model


def member_has_own_model(db: Session, tenant_id: str, member_agent_id: str) -> bool:
    """成员是否配了自己的模型（编辑页展示「用谁的模型」，也让用户预期成本）。"""
    binding = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == member_agent_id,
            AgentModelBinding.role == "default",
        )
    ).first()
    if binding is None:
        return False
    row = db.get(ModelConfig, binding.model_config_id)
    return row is not None and row.enabled


# ────────────────────────────────────────────────────────────
# 子会话
# ────────────────────────────────────────────────────────────


def child_session_id(parent_session_id: str, member_agent_id: str) -> str:
    """同一父会话 × 同一成员 → 同一个子会话 id（确定性，不需要额外查表）。

    这样同一成员连续被路由时复用会话，它自己的 QA 历史天然接得上；
    换了成员就换会话，不会把 A 的历史灌给 B。
    """
    digest = hashlib.sha256(f"{parent_session_id}|{member_agent_id}".encode("utf-8")).hexdigest()
    return f"dsp_{digest[:24]}"


def ensure_child_session(
    db: Session,
    parent: ChatSession,
    member: AgentProfile,
) -> ChatSession:
    """预创建子会话行。

    **必须预创建**（而不是让 AgentLoop 自建），一次解决两个坑：
    1. ``_get_or_create_session`` 只在 channel ∈ {pilotdeck_group_chat, skill_test} 时
       把 channel 落库，其它值一律 None → ``dispatch_child`` 会丢，子会话冒到侧边栏。
    2. 它的 ``if not chat_session`` 分支会 ``_bump_agent_usage(member)`` → 成员用量被路由虚高。
    """
    session_id = child_session_id(parent.id, member.id)
    child = db.get(ChatSession, session_id)
    if child is not None:
        metadata = dict(child.context_state_json or {})
        if metadata.get(DISPATCH_PARENT_KEY) != parent.id:
            metadata[DISPATCH_PARENT_KEY] = parent.id
            metadata["dispatch_dispatcher_agent_id"] = parent.agent_id
            child.context_state_json = metadata
            child.updated_at = utc_now()
            db.add(child)
        return child
    metadata = dict(parent.context_state_json or {})
    child = ChatSession(
        id=session_id,
        tenant_id=parent.tenant_id,
        user_id=parent.user_id,
        agent_id=member.id,
        channel=DISPATCH_CHILD_CHANNEL,
        title=None,
        context_state_json={
            DISPATCH_PARENT_KEY: parent.id,
            "dispatch_dispatcher_agent_id": parent.agent_id,
        },
    )
    db.add(child)
    db.flush()
    # 说明：这里**不** bump 成员用量 —— 用量归调度入口（见方案 §3.3）
    _ = metadata
    return child


def child_session_turn_id(parent_session_id: str, member_agent_id: str, client_turn_id: str | None) -> str:
    """子 turn 的 client_turn_id：同一轮同一成员唯一，便于取消与排查。"""
    base = (client_turn_id or parent_session_id).strip() or parent_session_id
    return f"{base}:{member_agent_id[:8]}"


# ────────────────────────────────────────────────────────────
# 上下文拼装（核心：只给转接记录，绝不给 QA 正文）
# ────────────────────────────────────────────────────────────


def read_dispatch_log(parent: ChatSession) -> list[dict[str, Any]]:
    raw = (parent.context_state_json or {}).get(DISPATCH_LOG_KEY)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def append_dispatch_log(db: Session, parent: ChatSession, entry: dict[str, Any]) -> None:
    """追加一条转接记录，截断到 DISPATCH_LOG_LIMIT 条。

    注意：**答案正文不进这里**。父会话的 assistant 消息只用于 UI 展示，
    不参与任何 prompt 构造（方案 §2）。
    """
    log = read_dispatch_log(parent)
    log.append(
        {
            "turn_id": str(entry.get("turn_id") or ""),
            "member_agent_id": str(entry.get("member_agent_id") or ""),
            "member_name": str(entry.get("member_name") or ""),
            "intent": str(entry.get("intent") or ""),
            "user_query": str(entry.get("user_query") or ""),
            "confidence": float(entry.get("confidence") or 0.0),
        }
    )
    metadata = dict(parent.context_state_json or {})
    metadata[DISPATCH_LOG_KEY] = log[-DISPATCH_LOG_LIMIT:]
    parent.context_state_json = metadata
    parent.updated_at = utc_now()
    db.add(parent)


def build_route_context(parent: ChatSession) -> str:
    """给路由 LLM 的「最近转接记录」。

    **只读 dispatch_log**，绝不读 assistant 正文 —— 这是上下文不被业务答案撑爆的关键，
    也是 test_dispatcher_context.py 的核心回归断言。
    """
    log = read_dispatch_log(parent)
    if not log:
        return ""
    lines = []
    for index, item in enumerate(log, start=1):
        query = str(item.get("user_query") or "").strip()
        name = str(item.get("member_name") or "").strip() or "未知同事"
        intent = str(item.get("intent") or "").strip()
        suffix = f"（{intent}）" if intent else ""
        lines.append(f"{index}. 「{query}」→ {name}{suffix}")
    return "最近转接记录：\n" + "\n".join(lines)


def build_child_context(
    parent: ChatSession,
    *,
    member_name: str,
    same_member: bool,
) -> str | None:
    """给子员工的 context_injection。

    同一成员：返回 None —— 子会话自己的历史已经够了，重复灌摘要只会污染上下文。
    换了成员：给几行转接摘要，让新同事知道前面聊过什么，但**不含任何 QA 正文**。
    """
    if same_member:
        return None
    log = read_dispatch_log(parent)
    if not log:
        return None
    lines = []
    for index, item in enumerate(log, start=1):
        query = str(item.get("user_query") or "").strip()
        name = str(item.get("member_name") or "").strip() or "其他同事"
        if not query:
            continue
        lines.append(f"{index}. 「{query}」→ {name}")
    if not lines:
        return None
    return (
        "【转接上下文】本会话此前的转接记录：\n"
        + "\n".join(lines)
        + f"\n现在由你（{member_name}）接手。用户这句可能接着上面的话题，"
        "如果指代不清，可以先确认再回答。"
    )


def last_routed_member_id(parent: ChatSession) -> str | None:
    log = read_dispatch_log(parent)
    for item in reversed(log):
        member_id = str(item.get("member_agent_id") or "").strip()
        if member_id:
            return member_id
    return None


def default_clarify_text(config: DispatchConfig) -> str:
    return config.fallback_reply or DEFAULT_CLARIFY_TEXT or DEFAULT_FALLBACK_REPLY


def candidates_text(
    members: list[tuple[DispatcherMember, AgentProfile]],
) -> str:
    """clarify 时列出的候选同事（纯文本，P2 再升级成 A2UI 卡片）。"""
    lines = []
    for index, (_member, agent) in enumerate(members, start=1):
        description = (agent.description or "").strip()
        suffix = f"：{description}" if description else ""
        lines.append(f"{index}. {agent.name}{suffix}")
    return "\n".join(lines)