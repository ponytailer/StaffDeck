"""员工转让：所有权换人 + 员工私有资源一并转名。

覆盖的边界：
- 谁能转（当前 owner / 管理员），谁不能转（同租户的其他人）；
- 转给谁（同租户正式成员），不能转给访客 / 别租户 / 自己；
- 转什么（员工私有知识库、通用技能、SOP 分支、独占工具），
  不转什么（个人知识库、开放广场资源、与别人员工共享的工具）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import agent_private_metadata
from app.agents.schema import AgentProfileUpdateRequest, AgentTransferRequest
from app.api.agents import transfer_agent, update_agent
from app.db.models import (
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    GeneralSkill,
    KnowledgeBase,
    Skill,
    Tenant,
    Tool,
    User,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_users(db: Session) -> tuple[User, User, User, User]:
    """owner / other / admin / guest（分享访客）。"""
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(Tenant(id="tenant_other", name="Other"))
    owner = User(
        id="user_owner",
        tenant_id="tenant_demo",
        username="owner",
        display_name="Owner",
        password_hash="x",
    )
    other = User(
        id="user_other",
        tenant_id="tenant_demo",
        username="other",
        display_name="Other",
        password_hash="x",
    )
    admin = User(
        id="user_admin",
        tenant_id="tenant_demo",
        username="admin",
        display_name="Admin",
        role="admin",
        password_hash="x",
    )
    guest = User(
        id="user_guest",
        tenant_id="tenant_demo",
        username="guest",
        display_name="Guest",
        source="share",
        password_hash="x",
    )
    outsider = User(
        id="user_outsider",
        tenant_id="tenant_other",
        username="outsider",
        display_name="Outsider",
        password_hash="x",
    )
    for row in (owner, other, admin, guest, outsider):
        db.add(row)
    db.commit()
    return owner, other, admin, guest


def _seed_employee(db: Session, owner: User) -> AgentProfile:
    agent = AgentProfile(
        id="agent_1",
        tenant_id="tenant_demo",
        name="研发员工",
        is_overall=False,
        metadata_json={
            "owner_user_id": owner.id,
            "owner_username": owner.username,
            "owner_display_name": owner.display_name,
            "created_by_user_id": owner.id,
            "created_by_username": owner.username,
            "created_by": owner.username,
            "created_by_display_name": owner.display_name,
            "creator_name": owner.username,
        },
    )
    db.add(agent)
    db.commit()
    return agent


def _bind(db: Session, agent_id: str, resource_type: str, resource_id: str, metadata=None):
    db.add(
        AgentResourceBinding(
            tenant_id="tenant_demo",
            agent_id=agent_id,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=metadata or {},
        )
    )


def _private_kb(db: Session, kb_id: str, owner: User, agent_id: str = "agent_1") -> KnowledgeBase:
    row = KnowledgeBase(
        id=kb_id,
        tenant_id="tenant_demo",
        name=f"库-{kb_id}",
        metadata_json=agent_private_metadata(
            agent_id,
            {"owner_user_id": owner.id, "owner_username": owner.username},
        ),
    )
    db.add(row)
    return row


def test_owner_transfers_employee_and_private_resources() -> None:
    with _test_session() as db:
        owner, other, _admin, _guest = _seed_users(db)
        agent = _seed_employee(db, owner)

        _private_kb(db, "kb_private", owner)
        _bind(db, agent.id, "knowledge_base", "kb_private")

        # 个人知识库：不属于员工，即使被误绑也不该被转走
        personal = KnowledgeBase(
            id="kb_personal",
            tenant_id="tenant_demo",
            name="我的个人库",
            metadata_json={
                "scope": "personal",
                "visibility": "personal",
                "personal": True,
                "owner_user_id": owner.id,
            },
        )
        db.add(personal)
        _bind(db, agent.id, "knowledge_base", "kb_personal")

        # 开放广场技能：全租户共享，不跟着员工走
        gallery_skill = GeneralSkill(
            id="gs_gallery",
            tenant_id="tenant_demo",
            slug="gallery-skill",
            name="广场技能",
            skill_markdown="# hi",
            metadata_json={"scope": "open_gallery", "owner_user_id": owner.id},
        )
        db.add(gallery_skill)
        _bind(db, agent.id, "general_skill", "gs_gallery")

        # 员工私有技能：跟着走
        private_skill = GeneralSkill(
            id="gs_private",
            tenant_id="tenant_demo",
            slug="private-skill",
            name="私有技能",
            skill_markdown="# hi",
            metadata_json=agent_private_metadata(
                agent.id, {"owner_user_id": owner.id}
            ),
        )
        db.add(private_skill)
        _bind(db, agent.id, "general_skill", "gs_private")

        # SOP：归属在员工分支上
        skill = Skill(
            id="skill_1",
            tenant_id="tenant_demo",
            skill_id="sop-1",
            name="报销 SOP",
            content_json={},
            status="published",
        )
        db.add(skill)
        db.add(
            AgentSkillBranch(
                id="branch_1",
                tenant_id="tenant_demo",
                agent_id=agent.id,
                skill_id="sop-1",
                source_skill_id="skill_1",
                content_json={},
                metadata_json=agent_private_metadata(
                    agent.id, {"owner_user_id": owner.id}
                ),
            )
        )
        _bind(db, agent.id, "skill", "skill_1")

        # 独占工具：归属在绑定行上
        db.add(
            Tool(
                id="tool_private",
                tenant_id="tenant_demo",
                name="独占工具",
                method="GET",
                url="https://example.com",
            )
        )
        _bind(
            db,
            agent.id,
            "tool",
            "tool_private",
            {"owner_user_id": owner.id, "owner_username": owner.username},
        )

        # 与另一个员工共享的工具：不能改名
        db.add(
            AgentProfile(
                id="agent_2", tenant_id="tenant_demo", name="市场员工", is_overall=False
            )
        )
        db.add(
            Tool(
                id="tool_shared",
                tenant_id="tenant_demo",
                name="共享工具",
                method="GET",
                url="https://example.com",
            )
        )
        _bind(
            db,
            agent.id,
            "tool",
            "tool_shared",
            {"owner_user_id": owner.id, "owner_username": owner.username},
        )
        _bind(
            db,
            "agent_2",
            "tool",
            "tool_shared",
            {"owner_user_id": owner.id, "owner_username": owner.username},
        )
        db.commit()

        result = transfer_agent(
            agent.id,
            AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=other.id),
            db=db,
            current_user=owner,
        )

        transferred = db.get(AgentProfile, agent.id)
        metadata = transferred.metadata_json or {}
        assert metadata["owner_user_id"] == other.id
        assert metadata["owner_username"] == "other"
        assert metadata["owner_display_name"] == "Other"
        # 卡片上的「@创建者」跟着新 owner，不然会显示成旧主人
        assert metadata["created_by_display_name"] == "Other"
        # 上一手留档
        assert metadata["previous_owner_user_id"] == owner.id
        assert metadata["previous_owner_username"] == "owner"
        assert result.owner_username == "other"

        assert result.transferred_resources == 4
        assert result.transferred_breakdown == {
            "knowledge_base": 1,
            "general_skill": 1,
            "skill": 1,
            "tool": 1,
        }
        assert result.agent.metadata["owner_user_id"] == other.id

        private_kb = db.get(KnowledgeBase, "kb_private")
        assert private_kb.metadata_json["owner_user_id"] == other.id
        assert private_kb.metadata_json["owner_agent_id"] == agent.id
        personal_kb = db.get(KnowledgeBase, "kb_personal")
        assert personal_kb.metadata_json["owner_user_id"] == owner.id

        private_skill_row = db.get(GeneralSkill, "gs_private")
        assert private_skill_row.metadata_json["owner_user_id"] == other.id
        gallery_skill_row = db.get(GeneralSkill, "gs_gallery")
        assert gallery_skill_row.metadata_json["owner_user_id"] == owner.id

        branch = db.get(AgentSkillBranch, "branch_1")
        assert branch.metadata_json["owner_user_id"] == other.id

        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_type == "tool"
            )
        ).all()
        owners = {(row.resource_id, row.agent_id): row.metadata_json.get("owner_user_id") for row in bindings}
        assert owners[("tool_private", agent.id)] == other.id
        # 共享工具两边都还是原 owner
        assert owners[("tool_shared", agent.id)] == owner.id
        assert owners[("tool_shared", "agent_2")] == owner.id


def test_only_owner_or_admin_can_transfer() -> None:
    with _test_session() as db:
        owner, other, admin, _guest = _seed_users(db)
        agent = _seed_employee(db, owner)

        with pytest.raises(HTTPException) as denied:
            transfer_agent(
                agent.id,
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=other.id),
                db=db,
                current_user=other,
            )
        assert denied.value.status_code == 403

        # 管理员可以代替 owner 转给第三人
        result = transfer_agent(
            agent.id,
            AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=other.id),
            db=db,
            current_user=admin,
        )
        assert result.agent.metadata["owner_user_id"] == other.id


def test_transfer_rejects_guest_other_tenant_and_self() -> None:
    with _test_session() as db:
        owner, other, _admin, guest = _seed_users(db)
        agent = _seed_employee(db, owner)

        with pytest.raises(HTTPException) as guest_error:
            transfer_agent(
                agent.id,
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=guest.id),
                db=db,
                current_user=owner,
            )
        assert guest_error.value.status_code == 400

        with pytest.raises(HTTPException) as tenant_error:
            transfer_agent(
                agent.id,
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id="user_outsider"),
                db=db,
                current_user=owner,
            )
        assert tenant_error.value.status_code == 404

        with pytest.raises(HTTPException) as self_error:
            transfer_agent(
                agent.id,
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=owner.id),
                db=db,
                current_user=owner,
            )
        assert self_error.value.status_code == 400

        with pytest.raises(HTTPException) as missing_error:
            transfer_agent(
                agent.id,
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id="user_nope"),
                db=db,
                current_user=owner,
            )
        assert missing_error.value.status_code == 404


def test_overall_agent_cannot_be_transferred() -> None:
    with _test_session() as db:
        _owner, other, admin, _guest = _seed_users(db)
        db.add(
            AgentProfile(
                id="agent_overall", tenant_id="tenant_demo", name="开放广场", is_overall=True
            )
        )
        db.commit()

        with pytest.raises(HTTPException) as error:
            transfer_agent(
                "agent_overall",
                AgentTransferRequest(tenant_id="tenant_demo", owner_user_id=other.id),
                db=db,
                current_user=admin,
            )
        assert error.value.status_code == 400


def test_generic_update_still_cannot_change_owner() -> None:
    """编辑资料不该顺手改归属：转让是唯一入口。"""
    with _test_session() as db:
        owner, other, _admin, _guest = _seed_users(db)
        agent = _seed_employee(db, owner)

        updated = update_agent(
            agent.id,
            AgentProfileUpdateRequest(
                tenant_id="tenant_demo",
                metadata={"owner_user_id": other.id, "owner_username": other.username},
            ),
            db=db,
            current_user=owner,
        )
        assert updated.metadata["owner_user_id"] == owner.id
        assert updated.metadata["owner_username"] == owner.username
