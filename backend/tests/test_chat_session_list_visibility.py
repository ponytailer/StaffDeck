"""对话页侧边栏的会话可见性。

口径：Public API（第三方 / AI 小程序用 API Key）创建的会话不该出现在站内人的
会话列表里——它们由外部系统托管，混进来会把侧边栏刷满「新会话」。会话本身仍能
按 id 打开/查询，只是不在 `GET /api/chat/sessions` 里列出。
"""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.chat import list_chat_sessions
from app.db.models import AgentProfile, ChatSession, Tenant, User
from app.security.auth import hash_password
from app.security.permissions import ADMIN_ROLE
from app.session.origin import DISPATCH_CHILD_CHANNEL, PUBLIC_API_SESSION_CHANNEL

TENANT = "tenant_demo"
ADMIN_ID = "user_admin"
AGENT_ID = "agent_demo"


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session) -> User:
    db.add(Tenant(id=TENANT, name="Demo"))
    admin = User(
        id=ADMIN_ID,
        tenant_id=TENANT,
        username="admin",
        password_hash=hash_password("admin"),
        role=ADMIN_ROLE,
    )
    db.add(admin)
    db.add(
        AgentProfile(
            id=AGENT_ID,
            tenant_id=TENANT,
            name="重庆 AI G.O",
            status="active",
        )
    )
    for index, channel in enumerate((None, PUBLIC_API_SESSION_CHANNEL, "skill_test")):
        db.add(
            ChatSession(
                id=f"session_{index}",
                tenant_id=TENANT,
                user_id=ADMIN_ID,
                agent_id=AGENT_ID,
                title=f"会话 {index}",
                channel=channel,
            )
        )
    db.commit()
    db.refresh(admin)
    return admin


def test_public_api_sessions_are_not_listed_in_chat_sidebar() -> None:
    db = _session()
    admin = _seed(db)

    rows = list_chat_sessions(tenant_id=TENANT, current_user=admin, db=db)

    assert [row.id for row in rows] == ["session_0"]


def test_public_api_session_is_still_readable_by_id() -> None:
    db = _session()
    _seed(db)

    row = db.get(ChatSession, "session_1")
    assert row is not None
    assert row.channel == PUBLIC_API_SESSION_CHANNEL


def test_dispatch_child_sessions_are_not_listed() -> None:
    """调度入口的子会话是「员工自己的工作区」，不该混进侧边栏。

    子会话按 (父会话 × 成员) 确定性生成、会长期存在，一旦冒到侧边栏
    用户列表里就会多出一堆自己没见过的会话。
    """
    db = _session()
    admin = _seed(db)
    db.add(
        ChatSession(
            id="session_child",
            tenant_id=TENANT,
            user_id=ADMIN_ID,
            agent_id=AGENT_ID,
            title="子员工的工作区",
            channel=DISPATCH_CHILD_CHANNEL,
        )
    )
    db.commit()

    rows = list_chat_sessions(tenant_id=TENANT, current_user=admin, db=db)

    assert [row.id for row in rows] == ["session_0"]
    # 但按 id 仍然读得到（取消联动、答案回溯都依赖它）
    assert db.get(ChatSession, "session_child") is not None
