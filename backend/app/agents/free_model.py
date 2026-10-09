"""全局免费模型:管理员标记一条模型 + 标记免费员工,成员无自己模型时自动使用。

口径(2026-10-08 产品确认):
- 配额按「用户 × 员工 × 天」计,每个免费员工各 FREE_MODEL_DAILY_LIMIT 问;
- 只在用户**没有自己的模型**时自动生效,有自己模型的用户完全不受影响;
- 全局模型租户内单选,且只能是管理员自己的模型(标记接口校验 ownership);
- 超限直接报错(不回落到其他模型),由前端译成友好提示。

并发:配额占用走 SQL 侧 ``INSERT .. ON CONFLICT DO UPDATE .. WHERE count < limit``
原子自增(PG 与 SQLite>=3.24 都支持),不做 read-modify-write。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlmodel import Session, select

from app.db.models import AgentProfile, GlobalFreeModelUsage, ModelConfig, utc_now

FREE_MODEL_DAILY_LIMIT = 10

# 超限错误码:前端按这个码译提示语,不要改
FREE_MODEL_QUOTA_EXHAUSTED_CODE = "free_model_quota_exhausted"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def get_global_free_model(db: Session, tenant_id: str) -> ModelConfig | None:
    """租户内当前生效的全局免费模型(单选 + enabled)。"""
    return db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_global_free == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()


def clear_global_free_flag(db: Session, tenant_id: str, exclude_config_id: str | None = None) -> None:
    """清掉租户内的全局标记(置新标记前调用,保证单选)。"""
    statement = select(ModelConfig).where(
        ModelConfig.tenant_id == tenant_id,
        ModelConfig.is_global_free == True,  # noqa: E712
    )
    if exclude_config_id:
        statement = statement.where(ModelConfig.id != exclude_config_id)
    for row in db.exec(statement).all():
        row.is_global_free = False
        row.updated_at = utc_now()
        db.add(row)


def is_agent_free_enabled(db: Session, tenant_id: str, agent_id: str | None) -> bool:
    """员工是否被标记为免费(无效 agent / 非本租户一律 False)。"""
    if not agent_id:
        return False
    agent = db.get(AgentProfile, agent_id)
    if not agent or agent.tenant_id != tenant_id:
        return False
    return bool(agent.free_model_enabled)


def own_model_for_role(db: Session, tenant_id: str, user_id: str, role: str) -> ModelConfig | None:
    """用户自己的 role 模型(不回落租户)。

    intent_recognition 角色没有专用标记时回落自己的默认模型——
    「有自己的模型就完全不受免费链路影响」的口径对任意角色都成立。
    """
    if role == "intent_recognition":
        own_intent = db.exec(
            select(ModelConfig).where(
                ModelConfig.tenant_id == tenant_id,
                ModelConfig.is_intent_recognition == True,  # noqa: E712
                ModelConfig.enabled == True,  # noqa: E712
                ModelConfig.user_id == user_id,
            )
        ).first()
        if own_intent is not None:
            return own_intent
    return db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
            ModelConfig.user_id == user_id,
        )
    ).first()


def resolve_turn_default_model(
    db: Session, tenant_id: str, agent_id: str | None, user_id: str | None, role: str = "default"
) -> ModelConfig | None:
    """对话回合的免费链路解析(仅.default 之外的各角色也统一走这里)。

    返回非 None 时调用方直接用该模型;返回 None 表示免费链路不生效,
    由调用方走原有 model_for_agent 回落(用户自己的 → 租户默认)。
    优先级:用户自己的 role 模型 > 全局免费模型;员工未标记/无效一律 None。
    """
    if not user_id or not agent_id:
        return None
    agent = db.get(AgentProfile, agent_id)
    if (
        not agent
        or agent.tenant_id != tenant_id
        or agent.status != "active"
        or not agent.free_model_enabled
    ):
        return None
    own = own_model_for_role(db, tenant_id, user_id, role)
    if own is not None:
        return own
    return get_global_free_model(db, tenant_id)


def used_today(db: Session, tenant_id: str, user_id: str, agent_id: str) -> int:
    row = db.exec(
        select(GlobalFreeModelUsage).where(
            GlobalFreeModelUsage.tenant_id == tenant_id,
            GlobalFreeModelUsage.user_id == user_id,
            GlobalFreeModelUsage.agent_id == agent_id,
            GlobalFreeModelUsage.usage_date == _today(),
        )
    ).first()
    return int(row.count) if row else 0


def remaining_free_quota(db: Session, tenant_id: str, user_id: str, agent_id: str) -> int:
    return max(0, FREE_MODEL_DAILY_LIMIT - used_today(db, tenant_id, user_id, agent_id))


def consume_free_quota(db: Session, tenant_id: str, user_id: str, agent_id: str) -> bool:
    """原子占用一问。返回 False 表示今日配额已用完(或并发抢占失败)。

    成功时立即 commit —— 配额计数与对话回合的事务解耦:问题已经"问了",
    即使回合后续失败也不退还(与真实 LLM 成本一致)。
    """
    now = utc_now()
    result = db.execute(
        text(
            """
            INSERT INTO global_free_model_usage
                (id, tenant_id, user_id, agent_id, usage_date, count, updated_at)
            VALUES
                (:id, :tenant_id, :user_id, :agent_id, :usage_date, 1, :updated_at)
            ON CONFLICT (tenant_id, user_id, agent_id, usage_date)
            DO UPDATE SET count = global_free_model_usage.count + 1, updated_at = :updated_at
            WHERE global_free_model_usage.count < :limit
            RETURNING count
            """
        ),
        {
            "id": f"freemodel_{tenant_id}_{user_id}_{agent_id}_{_today()}",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "usage_date": _today(),
            "updated_at": now,
            "limit": FREE_MODEL_DAILY_LIMIT,
        },
    )
    row = result.first()
    if row is None:
        # ON CONFLICT 的 WHERE 没放行 = 今日已到上限
        return False
    db.commit()
    return True


def free_model_turn_allowed(db: Session, tenant_id: str, user_id: str, agent_id: str | None) -> tuple[ModelConfig, None] | tuple[None, str]:
    """发送入口的免费链路预检:返回 (全局模型, None) 或 (None, 错误码)。"""
    if not is_agent_free_enabled(db, tenant_id, agent_id):
        return None, "agent_not_free"
    model = get_global_free_model(db, tenant_id)
    if not model:
        return None, "no_global_free_model"
    if remaining_free_quota(db, tenant_id, user_id, agent_id) <= 0:
        return None, FREE_MODEL_QUOTA_EXHAUSTED_CODE
    return model, None


def agent_free_summary(db: Session, tenant_id: str, agent_ids: list[str], user_id: str) -> dict[str, Any]:
    """批量汇总免费状态(员工广场列表用):一次查配额表,避免逐行 N+1。"""
    today = _today()
    counts: dict[str, int] = {}
    if agent_ids:
        rows = db.exec(
            select(GlobalFreeModelUsage).where(
                GlobalFreeModelUsage.tenant_id == tenant_id,
                GlobalFreeModelUsage.user_id == user_id,
                GlobalFreeModelUsage.agent_id.in_(agent_ids),
                GlobalFreeModelUsage.usage_date == today,
            )
        ).all()
        counts = {row.agent_id: int(row.count) for row in rows}
    return {
        agent_id: {
            "used": counts.get(agent_id, 0),
            "remaining": max(0, FREE_MODEL_DAILY_LIMIT - counts.get(agent_id, 0)),
        }
        for agent_id in agent_ids
    }
