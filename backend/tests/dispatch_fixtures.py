"""调度入口测试的共享夹具。

只放「造数据」相关的东西：内存库、租户/用户/员工/模型，以及一个可控的
路由替身。断言留在各个 test_dispatcher_*.py 里，避免一个文件越写越杂。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    AgentModelBinding,
    AgentProfile,
    ModelConfig,
    Tenant,
    User,
)
from app.dispatch.schema import DispatchConfig, DispatcherMemberInput
from app.dispatch.service import DISPATCH_KIND, write_dispatch_config
from app.security.permissions import ADMIN_ROLE

TENANT = "tenant_demo"
ADMIN_ID = "user_admin"
OWNER_ID = "user_owner"
OTHER_ID = "user_other"
DISPATCHER_ID = "agent_dispatcher"
MEMBER_A = "agent_hr"
MEMBER_B = "agent_it"
MEMBER_C = "agent_finance"


def memory_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def make_user(user_id: str, *, role: str = "member", tenant_id: str = TENANT) -> User:
    return User(
        id=user_id,
        tenant_id=tenant_id,
        username=user_id,
        display_name=user_id,
        role=role,
        password_hash="test",
    )


def make_model_config(
    model_id: str,
    *,
    user_id: str | None = None,
    enabled: bool = True,
    is_default: bool = True,
    is_global_free: bool = False,
    tenant_id: str = TENANT,
) -> ModelConfig:
    """``trust_status`` 留空 + revision=1 → 走 resolver 的 legacy 兼容分支。

    不这么做的话每造一个模型还要编一份指纹，测试重点会偏到模型校验上去。
    """
    return ModelConfig(
        id=model_id,
        tenant_id=tenant_id,
        user_id=user_id,
        name=f"model-{model_id}",
        model="deepseek-v4-flash",
        api_key_encrypted="encrypted",
        enabled=enabled,
        is_default=is_default,
        is_global_free=is_global_free,
    )


def bind_model(db: Session, agent_id: str, model_id: str) -> None:
    db.add(
        AgentModelBinding(
            tenant_id=TENANT,
            agent_id=agent_id,
            role="default",
            model_config_id=model_id,
        )
    )


def make_agent(
    agent_id: str,
    *,
    name: str,
    kind: str = "standard",
    status: str = "active",
    owner_id: str | None = None,
    tenant_id: str = TENANT,
    description: str | None = None,
) -> AgentProfile:
    metadata: dict[str, Any] = {}
    if owner_id:
        metadata["owner_user_id"] = owner_id
    return AgentProfile(
        id=agent_id,
        tenant_id=tenant_id,
        name=name,
        description=description,
        kind=kind,
        status=status,
        metadata_json=metadata,
    )


def make_dispatcher(
    agent_id: str = DISPATCHER_ID,
    *,
    name: str = "前台调度",
    owner_id: str = OWNER_ID,
    config: DispatchConfig | None = None,
    status: str = "active",
) -> AgentProfile:
    row = make_agent(
        agent_id,
        name=name,
        kind=DISPATCH_KIND,
        owner_id=owner_id,
        status=status,
    )
    write_dispatch_config(row, config or DispatchConfig())
    return row


def member_input(
    agent_id: str,
    *,
    rule: str = "",
    examples: list[str] | None = None,
    sort_order: int = 0,
) -> DispatcherMemberInput:
    return DispatcherMemberInput(
        member_agent_id=agent_id,
        routing_rule=rule,
        example_queries=list(examples or []),
        sort_order=sort_order,
    )


def seed_tenant(db: Session) -> None:
    """租户 + 管理员 + 创建者 + 一个外人。"""
    db.add(Tenant(id=TENANT, name="Demo"))
    db.add(make_user(ADMIN_ID, role=ADMIN_ROLE))
    db.add(make_user(OWNER_ID))
    db.add(make_user(OTHER_ID))
    db.commit()


def seed_two_members(db: Session) -> None:
    """两个 active 的普通员工（人手一个模型，便于验证「谁配谁付费」）。

    成员都挂 ``OWNER_ID`` 名下：``_ensure_members_visible`` 要求成员对当前用户可见，
    真实场景里能选进调度入口的员工本来也都是自己建的。
    """
    db.add(
        make_agent(
            MEMBER_A,
            name="入职小助手",
            description="办理入职、工牌、门禁",
            owner_id=OWNER_ID,
        )
    )
    db.add(
        make_agent(
            MEMBER_B,
            name="IT 支持",
            description="电脑、网络、账号权限",
            owner_id=OWNER_ID,
        )
    )
    db.add(make_model_config("model_hr", user_id=OWNER_ID))
    db.add(make_model_config("model_it", user_id=OWNER_ID))
    bind_model(db, MEMBER_A, "model_hr")
    bind_model(db, MEMBER_B, "model_it")
    db.commit()


def resolved_model(model_id: str) -> SimpleNamespace:
    """给 router / stream 用的轻量模型替身（只读 id 与 model 名）。"""
    return SimpleNamespace(
        id=model_id,
        tenant_id=TENANT,
        api_protocol="openai_chat_completions",
        base_url="https://example.invalid",
        api_key_encrypted="encrypted",
        model="deepseek-v4-flash",
        temperature=0.2,
        max_output_tokens=4096,
        protocol_options={},
        legacy_extra_body={},
        custom_headers={},
        config_revision=1,
        security_revision=1,
        purpose="runtime",
        timeout_seconds=None,
    )


class StubRouter:
    """记录入参、返回预设决策的路由替身。"""

    def __init__(self, decision: Any) -> None:
        self.decision = decision
        self.calls: list[dict[str, Any]] = []

    def decide(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.decision