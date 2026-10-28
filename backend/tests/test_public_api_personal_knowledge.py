from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.agents.branching import ensure_knowledge_base_version
from app.api.auth import (
    AccountAPICredentialCreateRequest,
    create_account_api_credential,
)
from app.api.knowledge_bases import create_knowledge_base
from app.db import get_session
from app.db.models import (
    APIClient,
    APICredential,
    AgentProfile,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Tenant,
    User,
)
from app.knowledge.schema import KnowledgeBaseCreateRequest, KnowledgeBaseUpdateRequest
from app.api.knowledge_bases import update_knowledge_base
from app.public_api.app import create_public_api_app
from app.public_api.auth import generate_api_key
from app.public_api.credential_profiles import AGENT_FULL_ACCESS_SCOPES


def _app(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_api", name="API Tenant"))
        owner = User(
            id="user_owner",
            tenant_id="tenant_api",
            username="owner",
            role="member",
            password_hash="x",
        )
        other = User(
            id="user_other",
            tenant_id="tenant_api",
            username="other",
            role="member",
            password_hash="x",
        )
        db.add(owner)
        db.add(other)
        db.add(
            AgentProfile(
                id="agent_api",
                tenant_id="tenant_api",
                name="API Employee",
                status="active",
                is_overall=False,
                metadata_json={"owner_user_id": owner.id, "owner_username": owner.username},
            )
        )
        db.commit()

        knowledge_base = create_knowledge_base(
            KnowledgeBaseCreateRequest(
                tenant_id="tenant_api", name="我的报销依据", description="Q&A 依据"
            ),
            None,
            db,
            owner,
            mine=True,
        )
        version = ensure_knowledge_base_version(db, db.get(KnowledgeBase, knowledge_base.id))
        document = KnowledgeDocument(
            tenant_id="tenant_api",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            filename="差旅报销.md",
            file_type="md",
            title="差旅报销",
            status="ready",
            bucket_count=1,
            chunk_count=1,
        )
        bucket = KnowledgeBucket(
            tenant_id="tenant_api",
            knowledge_base_id=knowledge_base.id,
            knowledge_base_version_id=version.id,
            document_id=document.id,
            bucket_key="travel",
            title="差旅住宿标准",
            summary="差旅住宿标准说明",
        )
        db.add(document)
        db.add(bucket)
        db.flush()
        db.add(
            KnowledgeChunk(
                tenant_id="tenant_api",
                knowledge_base_id=knowledge_base.id,
                knowledge_base_version_id=version.id,
                document_id=document.id,
                bucket_id=bucket.id,
                chunk_index=0,
                content="差旅住宿标准每晚不超过 600 元，超标需分管领导审批。",
            )
        )

        # 第二个个人库：用来验证「一次调用覆盖全部个人库」
        handbook = create_knowledge_base(
            KnowledgeBaseCreateRequest(
                tenant_id="tenant_api", name="产品手册", description="产品说明"
            ),
            None,
            db,
            owner,
            mine=True,
        )
        handbook_version = ensure_knowledge_base_version(db, db.get(KnowledgeBase, handbook.id))
        handbook_document = KnowledgeDocument(
            tenant_id="tenant_api",
            knowledge_base_id=handbook.id,
            knowledge_base_version_id=handbook_version.id,
            filename="差旅产品.md",
            file_type="md",
            title="差旅产品",
            status="ready",
            bucket_count=1,
            chunk_count=1,
        )
        handbook_bucket = KnowledgeBucket(
            tenant_id="tenant_api",
            knowledge_base_id=handbook.id,
            knowledge_base_version_id=handbook_version.id,
            document_id=handbook_document.id,
            bucket_key="product",
            title="差旅住宿产品",
            summary="差旅住宿产品说明",
        )
        db.add(handbook_document)
        db.add(handbook_bucket)
        db.flush()
        db.add(
            KnowledgeChunk(
                tenant_id="tenant_api",
                knowledge_base_id=handbook.id,
                knowledge_base_version_id=handbook_version.id,
                document_id=handbook_document.id,
                bucket_id=handbook_bucket.id,
                chunk_index=0,
                content="差旅住宿产品支持按城市配置住宿标准，默认 600 元。",
            )
        )
        db.commit()

        owner_key = create_account_api_credential(
            AccountAPICredentialCreateRequest(name="owner key"),
            owner,
            db,
        ).api_key
        other_key = create_account_api_credential(
            AccountAPICredentialCreateRequest(name="other key"),
            other,
            db,
        ).api_key
        # 员工级密钥：绑定单个员工，只拿 runtime 作用域
        agent_client = APIClient(
            tenant_id="tenant_api",
            name="agent runtime client",
            created_by_user_id=owner.id,
            scopes_json=["*"],
        )
        db.add(agent_client)
        db.flush()
        agent_key, agent_prefix, agent_digest = generate_api_key()
        db.add(
            APICredential(
                tenant_id="tenant_api",
                client_id=agent_client.id,
                agent_id="agent_api",
                name="agent runtime",
                key_prefix=agent_prefix,
                key_digest=agent_digest,
                scopes_json=sorted(AGENT_FULL_ACCESS_SCOPES),
            )
        )
        db.commit()

    app = create_public_api_app()

    def session_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr("app.public_api.app.engine", engine)
    monkeypatch.setattr("app.public_api.jobs.enqueue_async_job", lambda *args, **kwargs: "queued")
    return TestClient(app), engine, {
        "owner_key": owner_key,
        "other_key": other_key,
        "agent_key": agent_key,
        "knowledge_base_id": knowledge_base.id,
        "knowledge_base_ids": [knowledge_base.id, handbook.id],
    }


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_personal_knowledge_is_exposed_to_account_keys_only(monkeypatch) -> None:
    client, _engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    listed = client.get("/knowledge-bases", headers=headers)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["data"]
    assert sorted(row["id"] for row in rows) == sorted(ctx["knowledge_base_ids"])
    assert "tenant_id" not in rows[0]

    detail = client.get(f"/knowledge-bases/{ctx['knowledge_base_id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["name"] == "我的报销依据"

    documents = client.get(
        f"/knowledge-bases/{ctx['knowledge_base_id']}/documents", headers=headers
    )
    assert documents.status_code == 200, documents.text
    assert [row["id"] for row in documents.json()["data"]]

    # 别人的密钥看不到、也拿不到详情
    other_headers = _auth(ctx["other_key"])
    assert client.get("/knowledge-bases", headers=other_headers).json()["data"] == []
    denied = client.get(
        f"/knowledge-bases/{ctx['knowledge_base_id']}", headers=other_headers
    )
    assert denied.status_code == 404
    assert denied.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"


def test_personal_knowledge_search_returns_grounding_payload(monkeypatch) -> None:
    client, _engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    response = client.post(
        f"/knowledge-bases/{ctx['knowledge_base_id']}:search",
        headers=headers,
        json={"query": "差旅住宿标准", "mode": "debug", "max_depth": 2},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["knowledge_base_id"] == ctx["knowledge_base_id"]
    assert payload["knowledge_base_version"] == "1.0.0"
    # 单库接口也回「库 → 版本」快照（做缓存失效）
    assert [item["id"] for item in payload["knowledge_bases"]] == [ctx["knowledge_base_id"]]
    assert payload["knowledge_bases"][0]["version"] == "1.0.0"
    serialized = str(payload)
    assert "差旅住宿标准" in serialized


def test_personal_knowledge_archived_and_foreign_scope_errors(monkeypatch) -> None:
    client, engine, ctx = _app(monkeypatch)

    # 员工级（非账号级）密钥一律拒绝
    forbidden = client.get("/knowledge-bases", headers=_auth(ctx["agent_key"]))
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ACCOUNT_KEY_REQUIRED"

    # 下线后 search 给出可区分错误码
    with Session(engine) as db:
        owner = db.get(User, "user_owner")
        assert owner is not None
        update_knowledge_base(
            ctx["knowledge_base_id"],
            KnowledgeBaseUpdateRequest(tenant_id="tenant_api", status="archived"),
            None,
            db,
            owner,
        )
    archived = client.post(
        f"/knowledge-bases/{ctx['knowledge_base_id']}:search",
        headers=_auth(ctx["owner_key"]),
        json={"query": "差旅住宿标准"},
    )
    assert archived.status_code == 409
    assert archived.json()["code"] == "KNOWLEDGE_BASE_ARCHIVED"

    # 列表仍返回该行（status=archived），外部可提示“已下线”
    listed = client.get("/knowledge-bases", headers=_auth(ctx["owner_key"]))
    assert listed.status_code == 200
    assert listed.json()["data"][0]["status"] == "archived"


def test_collection_search_covers_all_personal_knowledge_bases(monkeypatch) -> None:
    client, _engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    response = client.post(
        "/knowledge:search",
        headers=headers,
        json={"query": "差旅住宿标准", "mode": "debug", "max_depth": 2},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["query"] == "差旅住宿标准"
    # 不传 knowledge_base_ids = 名下全部个人库
    assert sorted(item["id"] for item in payload["knowledge_bases"]) == sorted(
        ctx["knowledge_base_ids"]
    )
    assert all(item["version"] == "1.0.0" for item in payload["knowledge_bases"])
    assert payload["chunks"]
    assert payload["citations"]


def test_collection_search_can_be_narrowed_to_one_library(monkeypatch) -> None:
    client, _engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    response = client.post(
        "/knowledge:search",
        headers=headers,
        json={
            "query": "差旅住宿标准",
            "knowledge_base_ids": [ctx["knowledge_base_id"]],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["id"] for item in payload["knowledge_bases"]] == [ctx["knowledge_base_id"]]


def test_collection_search_scope_errors(monkeypatch) -> None:
    client, engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    # 别人的库 / 不存在的库 → 404
    unknown = client.post(
        "/knowledge:search",
        headers=headers,
        json={"query": "差旅", "knowledge_base_ids": ["kb_does_not_exist"]},
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "KNOWLEDGE_BASE_NOT_FOUND"

    # 显式指定已下线的库 → 409
    with Session(engine) as db:
        owner = db.get(User, "user_owner")
        assert owner is not None
        update_knowledge_base(
            ctx["knowledge_base_id"],
            KnowledgeBaseUpdateRequest(tenant_id="tenant_api", status="archived"),
            None,
            db,
            owner,
        )
    archived = client.post(
        "/knowledge:search",
        headers=headers,
        json={"query": "差旅", "knowledge_base_ids": [ctx["knowledge_base_id"]]},
    )
    assert archived.status_code == 409
    assert archived.json()["code"] == "KNOWLEDGE_BASE_ARCHIVED"

    # 不指定 ids 时，已下线的库不会被检索（只剩另一个库）
    remaining = client.post("/knowledge:search", headers=headers, json={"query": "差旅"})
    assert remaining.status_code == 200, remaining.text
    remaining_ids = [item["id"] for item in remaining.json()["knowledge_bases"]]
    assert ctx["knowledge_base_id"] not in remaining_ids
    assert remaining_ids == [ctx["knowledge_base_ids"][1]]


def test_collection_search_validates_body_fields(monkeypatch) -> None:
    client, _engine, ctx = _app(monkeypatch)
    headers = _auth(ctx["owner_key"])

    # 内部字段不允许由调用方覆盖（否则会绕过范围/版本控制）
    rejected = client.post(
        "/knowledge:search",
        headers=headers,
        json={"query": "差旅", "tenant_id": "tenant_api", "mine": False},
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_ARGUMENT"

    # 单库接口同样拒绝内部字段（此前会因重复关键字直接 500）
    rejected_single = client.post(
        f"/knowledge-bases/{ctx['knowledge_base_id']}:search",
        headers=headers,
        json={"query": "差旅", "agent_id": "agent_x"},
    )
    assert rejected_single.status_code == 422

    bad_ids = client.post(
        "/knowledge:search",
        headers=headers,
        json={"query": "差旅", "knowledge_base_ids": "kb_1"},
    )
    assert bad_ids.status_code == 422


def test_personal_knowledge_openapi_paths_registered(monkeypatch) -> None:
    client, _engine, _ctx = _app(monkeypatch)
    schema = client.get("/openapi.json").json()
    expected = {
        "/knowledge-bases",
        "/knowledge-bases/{knowledge_base_id}",
        "/knowledge-bases/{knowledge_base_id}/documents",
        "/knowledge-bases/{knowledge_base_id}:search",
        "/knowledge:search",
    }
    assert expected.issubset(set(schema["paths"]))