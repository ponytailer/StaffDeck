from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.agents.branching import (
    ensure_knowledge_base_version,
    get_overall_agent,
    is_open_gallery_resource,
    visible_knowledge_base_versions,
)
from app.agents.schema import AgentResourceImportRequest
from app.api.agents import import_agent_resources
from app.api.knowledge import (
    get_document,
    list_documents,
    list_jobs,
    search_knowledge,
    update_document,
)
from app.api.knowledge_bases import (
    create_knowledge_base,
    delete_knowledge_base,
    get_knowledge_base,
    list_knowledge_bases,
    update_knowledge_base,
)
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Tenant,
    User,
)
from app.knowledge.schema import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseUpdateRequest,
    KnowledgeDocumentUpdateRequest,
    KnowledgeSearchRequest,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _user(user_id: str, *, role: str = "member") -> User:
    return User(
        id=user_id,
        tenant_id="tenant_demo",
        username=user_id,
        role=role,
        password_hash="x",
    )


def _seed(db: Session) -> tuple[User, User, AgentProfile]:
    owner = _user("user_owner")
    other = _user("user_other")
    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(owner)
    db.add(other)
    employee = AgentProfile(
        id="agent_emp",
        tenant_id="tenant_demo",
        name="报销助手",
        status="active",
        is_overall=False,
        metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
    )
    db.add(employee)
    db.commit()
    return owner, other, employee


def _seed_personal_document(db: Session, knowledge_base_id: str) -> KnowledgeDocument:
    knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
    assert knowledge_base is not None
    version = ensure_knowledge_base_version(db, knowledge_base)
    document = KnowledgeDocument(
        tenant_id="tenant_demo",
        knowledge_base_id=knowledge_base.id,
        knowledge_base_version_id=version.id,
        filename="报销制度.md",
        file_type="md",
        title="报销制度",
        status="ready",
        bucket_count=1,
        chunk_count=1,
    )
    bucket = KnowledgeBucket(
        tenant_id="tenant_demo",
        knowledge_base_id=knowledge_base.id,
        knowledge_base_version_id=version.id,
        document_id=document.id,
        bucket_key="expense",
        title="差旅报销",
        summary="差旅报销标准",
    )
    db.add(document)
    db.add(bucket)
    db.flush()
    db.add(
        KnowledgeChunk(
            tenant_id="tenant_demo",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="差旅住宿标准每晚不超过 600 元。",
        )
    )
    db.commit()
    return document


def _create_personal(owner: User, db: Session, name: str = "我的报销依据") -> KnowledgeBase:
    return create_knowledge_base(
        KnowledgeBaseCreateRequest(
            tenant_id="tenant_demo",
            name=name,
            description="个人知识库",
        ),
        None,
        db,
        owner,
        mine=True,
    )


def test_member_can_create_and_list_personal_knowledge_base() -> None:
    with _test_session() as db:
        owner, other, _employee = _seed(db)

        created = _create_personal(owner, db)
        assert created.name == "我的报销依据"
        db.refresh(db.get(KnowledgeBase, created.id))

        row = db.get(KnowledgeBase, created.id)
        assert row is not None
        metadata = row.metadata_json or {}
        assert metadata["visibility"] == "personal"
        assert metadata["scope"] == "personal"
        assert metadata["owner_user_id"] == owner.id
        # 个人库不建立任何绑定：因此既不在广场、也不被任何员工检索到
        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_id == created.id
            )
        ).all() == []
        assert is_open_gallery_resource(db, "tenant_demo", "knowledge_base", row) is False
        assert visible_knowledge_base_versions(db, "tenant_demo", None) == {}
        assert get_overall_agent(db, "tenant_demo") is None

        mine = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=None,
            db=db,
            mine=True,
            current_user=owner,
        )
        assert [item.id for item in mine["items"]] == [created.id]

        # 别人的「我的知识库」、以及广场视角都看不到它
        other_mine = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=None,
            db=db,
            mine=True,
            current_user=other,
        )
        assert other_mine["items"] == []
        plaza = list_knowledge_bases(tenant_id="tenant_demo", agent_id=None, db=db)
        assert plaza["items"] == []


def test_personal_knowledge_base_is_owner_only() -> None:
    with _test_session() as db:
        owner, other, _employee = _seed(db)
        created = _create_personal(owner, db)
        row = db.get(KnowledgeBase, created.id)

        assert get_knowledge_base(created.id, "tenant_demo", None, db, owner).id == created.id

        with pytest.raises(HTTPException) as denied:
            get_knowledge_base(created.id, "tenant_demo", None, db, other)
        assert denied.value.status_code == 404

        with pytest.raises(HTTPException) as denied:
            update_knowledge_base(
                created.id,
                KnowledgeBaseUpdateRequest(tenant_id="tenant_demo", name="翻新名字"),
                None,
                db,
                other,
            )
        assert denied.value.status_code == 404

        # 非属主的「新建」不受影响（这是新库，不是写本文库）
        assert row is not None and row.name == "我的报销依据"


def test_personal_knowledge_base_can_be_archived_but_not_deleted() -> None:
    with _test_session() as db:
        owner, _other, _employee = _seed(db)
        created = _create_personal(owner, db)

        archived = update_knowledge_base(
            created.id,
            KnowledgeBaseUpdateRequest(tenant_id="tenant_demo", status="archived"),
            None,
            db,
            owner,
        )
        assert archived.status == "archived"
        row = db.get(KnowledgeBase, created.id)
        assert row is not None and row.status == "archived"
        # 下线不会创建广场绑定
        assert db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.resource_id == created.id
            )
        ).all() == []

        mine = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=None,
            db=db,
            mine=True,
            hide_empty_default=False,
            current_user=owner,
        )
        assert [item.id for item in mine["items"]] == [created.id]
        assert mine["items"][0].status == "archived"

        with pytest.raises(HTTPException) as denied:
            delete_knowledge_base(created.id, "tenant_demo", None, db, owner)
        assert denied.value.status_code == 400


def test_personal_knowledge_documents_are_scoped_and_editable() -> None:
    with _test_session() as db:
        owner, other, _employee = _seed(db)
        created = _create_personal(owner, db)
        document = _seed_personal_document(db, created.id)

        page = list_documents(
            tenant_id="tenant_demo",
            knowledge_base_id=created.id,
            agent_id=None,
            page=1,
            page_size=50,
            db=db,
            mine=True,
            current_user=owner,
        )
        assert [item.id for item in page["items"]] == [document.id]

        other_page = list_documents(
            tenant_id="tenant_demo",
            knowledge_base_id=created.id,
            agent_id=None,
            page=1,
            page_size=50,
            db=db,
            mine=True,
            current_user=other,
        )
        assert other_page["items"] == []

        assert get_document(document.id, "tenant_demo", None, db, owner).id == document.id
        with pytest.raises(HTTPException) as denied:
            get_document(document.id, "tenant_demo", None, db, other)
        assert denied.value.status_code == 404

        updated = update_document(
            document.id,
            KnowledgeDocumentUpdateRequest(
                tenant_id="tenant_demo", title="报销制度（修订）"
            ),
            db,
            owner,
        )
        assert updated.title == "报销制度（修订）"

        # 个人库的入库任务只在 mine 口径下可见
        assert list_jobs(
            tenant_id="tenant_demo",
            agent_id=None,
            status=None,
            limit=8,
            db=db,
            mine=True,
            current_user=owner,
        ) == []


def test_personal_knowledge_import_creates_employee_branch_copy() -> None:
    with _test_session() as db:
        owner, _other, employee = _seed(db)
        created = _create_personal(owner, db)
        document = _seed_personal_document(db, created.id)

        result = import_agent_resources(
            employee.id,
            AgentResourceImportRequest(
                tenant_id="tenant_demo",
                resource_type="knowledge_base",
                resource_ids=[created.id],
                source_personal=True,
            ),
            db,
            owner,
        )
        assert result["source_personal"] is True
        assert [item["resource_id"] for item in result["imported"]] == [created.id]

        # 员工侧出现分支副本，并且检索可见
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.agent_id == employee.id,
                AgentKnowledgeBranch.knowledge_base_id == created.id,
            )
        ).first()
        assert branch is not None
        assert branch.sync_state == "synced"
        assert created.id in visible_knowledge_base_versions(db, "tenant_demo", employee.id)

        employee_page = list_knowledge_bases(
            tenant_id="tenant_demo",
            agent_id=employee.id,
            page=1,
            page_size=50,
            db=db,
            current_user=owner,
        )
        assert [item.id for item in employee_page["items"]] == [created.id]

        employee_docs = list_documents(
            tenant_id="tenant_demo",
            knowledge_base_id=created.id,
            agent_id=employee.id,
            page=1,
            page_size=50,
            db=db,
            current_user=owner,
        )
        assert [item.id for item in employee_docs["items"]] == [document.id]

        # 员工副本的改动不影响个人库本体
        version = ensure_knowledge_base_version(db, db.get(KnowledgeBase, created.id))
        assert version.id == document.knowledge_base_version_id


def test_personal_knowledge_import_rejects_other_users_library() -> None:
    with _test_session() as db:
        owner, other, employee = _seed(db)
        created = _create_personal(owner, db)
        _seed_personal_document(db, created.id)
        # 让 other 拥有这个员工，好让「导入目标」权限成立
        employee_row = db.get(AgentProfile, employee.id)
        assert employee_row is not None
        employee_row.metadata_json = {"owner_user_id": other.id, "owner_username": other.username}
        db.add(employee_row)
        db.commit()

        result = import_agent_resources(
            employee.id,
            AgentResourceImportRequest(
                tenant_id="tenant_demo",
                resource_type="knowledge_base",
                resource_ids=[created.id],
                source_personal=True,
            ),
            db,
            other,
        )
        assert result["imported"] == []
        assert result["missing"] == [
            {"resource_id": created.id, "reason": "resource_not_found"}
        ]


def test_personal_knowledge_search_rejects_archived_and_foreign_library() -> None:
    with _test_session() as db:
        owner, other, _employee = _seed(db)
        created = _create_personal(owner, db)
        _seed_personal_document(db, created.id)

        with pytest.raises(HTTPException) as denied:
            search_knowledge(
                KnowledgeSearchRequest(
                    tenant_id="tenant_demo",
                    mine=True,
                    knowledge_base_ids=[created.id],
                    query="差旅住宿标准",
                    mode="debug",
                ),
                db,
                other,
            )
        assert denied.value.status_code == 404

        update_knowledge_base(
            created.id,
            KnowledgeBaseUpdateRequest(tenant_id="tenant_demo", status="archived"),
            None,
            db,
            owner,
        )
        with pytest.raises(HTTPException) as denied:
            search_knowledge(
                KnowledgeSearchRequest(
                    tenant_id="tenant_demo",
                    mine=True,
                    knowledge_base_ids=[created.id],
                    query="差旅住宿标准",
                    mode="debug",
                ),
                db,
                owner,
            )
        assert denied.value.status_code == 409

        # 集合检索（不传 ids）只覆盖未下线的库：已下线的库直接不出现在范围里
        empty = search_knowledge(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                mine=True,
                query="差旅住宿标准",
                mode="debug",
            ),
            db,
            owner,
        )
        assert empty.chunks == []
        assert any(
            (item or {}).get("phase") == "no_visible_knowledge"
            for item in (empty.trace or [])
            if isinstance(item, dict)
        )

def test_personal_knowledge_http_surface(monkeypatch) -> None:
    """HTTP 层参数绑定：mine=1 必须真的走 FastAPI 的 query 解析。"""

    import base64

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlmodel import SQLModel, create_engine

    from app.api.knowledge import router as knowledge_router
    from app.api.knowledge_bases import router as knowledge_bases_router
    from app.db import get_session
    from app.security.auth import get_current_user

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
    # 依赖注入用的 principal 用裸 User 对象：只读取 id / tenant_id，避免 detached 刷新
    owner = _user("user_owner")
    other = _user("user_other")

    monkeypatch.setattr(
        "app.api.knowledge.schedule_knowledge_ingest", lambda *args, **kwargs: True
    )

    app = FastAPI()
    app.include_router(knowledge_bases_router)
    app.include_router(knowledge_router)

    def session_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override

    def as_user(user: User):
        app.dependency_overrides[get_current_user] = lambda: user

    client = TestClient(app)
    as_user(owner)

    # 「API 接入」指引：路径与对外基址都由后端给，避免前端自己拼
    access = client.get("/api/enterprise/knowledge-bases/api-access")
    assert access.status_code == 200, access.text
    assert access.json()["required_scope"] == "knowledge:read"
    assert access.json()["endpoint_path"] == "/api/v1/knowledge-bases/{knowledge_base_id}:search"
    assert access.json()["docs_path"] == "/api/v1/docs"

    created = client.post(
        "/api/enterprise/knowledge-bases?tenant_id=tenant_demo&mine=1",
        json={"tenant_id": "tenant_demo", "name": "个人测试库", "description": "HTTP"},
    )
    assert created.status_code == 200, created.text
    knowledge_base_id = created.json()["id"]
    assert created.json()["name"] == "个人测试库"

    listed = client.get("/api/enterprise/knowledge-bases?tenant_id=tenant_demo&mine=1")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [knowledge_base_id]

    plaza = client.get("/api/enterprise/knowledge-bases?tenant_id=tenant_demo")
    assert plaza.status_code == 200
    assert plaza.json()["items"] == []

    uploaded = client.post(
        "/api/enterprise/knowledge/documents?tenant_id=tenant_demo&mine=1",
        json={
            "tenant_id": "tenant_demo",
            "knowledge_base_id": knowledge_base_id,
            "filename": "制度.md",
            "title": "制度",
            "content_base64": base64.b64encode("差旅住宿标准 600 元。".encode()).decode(),
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    jobs = client.get("/api/enterprise/knowledge/jobs?tenant_id=tenant_demo&mine=1")
    assert jobs.status_code == 200
    assert [row["id"] for row in jobs.json()] == [uploaded.json()["id"]]

    # 个人库只允许下线
    denied_delete = client.delete(
        f"/api/enterprise/knowledge-bases/{knowledge_base_id}?tenant_id=tenant_demo"
    )
    assert denied_delete.status_code == 400

    archived = client.put(
        f"/api/enterprise/knowledge-bases/{knowledge_base_id}?tenant_id=tenant_demo",
        json={"tenant_id": "tenant_demo", "status": "archived"},
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "archived"

    as_user(other)
    forbidden = client.get(
        f"/api/enterprise/knowledge-bases/{knowledge_base_id}?tenant_id=tenant_demo"
    )
    assert forbidden.status_code == 404
    assert client.get(
        "/api/enterprise/knowledge-bases?tenant_id=tenant_demo&mine=1"
    ).json()["items"] == []
