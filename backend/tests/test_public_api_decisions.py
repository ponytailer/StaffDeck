"""开放 API：决策助手（Laya）端点的认证、授权与转发行为。"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.db.models import Tenant, User
from app.public_api.app import create_public_api_app
from app.public_api.credential_profiles import (
    AGENT_RUNTIME_SCOPES,
    USER_FULL_ACCESS_SCOPES,
)
from app.security.auth import create_access_token


PREDICT_PATH = "/decisions/predict"


def _payload() -> dict:
    return {
        "state": {"background": "客户来电反馈账单被重复扣款，明确要求退款"},
        "questions": {
            "q_dept": {
                "type": "choice",
                "instructions": "这条工单应由哪个部门处理？",
                "criteria": {"billing": "账单、扣款、退款", "technical": "Bug、报错"},
            },
            "q_refund": {"type": "noul", "instructions": "用户是否明确要求退款？"},
        },
    }


def _client(monkeypatch) -> tuple[TestClient, str]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Tenant(id="tenant_dec", name="Decision Tenant"))
        admin = User(
            id="user_dec_admin",
            tenant_id="tenant_dec",
            username="dec_admin",
            role="admin",
            password_hash="x",
        )
        db.add(admin)
        db.commit()
        token = create_access_token(admin)

    app = create_public_api_app()

    def session_override():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr("app.public_api.app.engine", engine)
    return TestClient(app), token


def _api_key(client: TestClient, admin_token: str, scopes: list[str]) -> str:
    created = client.post(
        "/api-clients",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "decisions", "scopes": ["*"]},
    )
    assert created.status_code == 201, created.text
    credential = client.post(
        f"/api-clients/{created.json()['id']}/credentials",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": "decision caller", "scopes": scopes},
    )
    assert credential.status_code == 201, credential.text
    return credential.json()["api_key"]


def _patch_upstream(monkeypatch, handler) -> list[httpx.Request]:
    """替换 Laya 出站，记录实际发往上游的请求（保留原始 httpx.Client 防递归）。"""
    seen: list[httpx.Request] = []
    real_client = httpx.Client

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(**kwargs) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(wrapped), timeout=kwargs.get("timeout"))

    monkeypatch.setattr("app.api.laya.httpx.Client", factory)
    return seen


def test_predict_endpoint_is_published_in_openapi(monkeypatch) -> None:
    client, _token = _client(monkeypatch)
    assert PREDICT_PATH in client.get("/openapi.json").json()["paths"]


def test_predict_requires_an_sd_api_key(monkeypatch) -> None:
    client, admin_token = _client(monkeypatch)

    anonymous = client.post(PREDICT_PATH, json=_payload())
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "NOT_AUTHENTICATED"

    # 用户 token 不是 API 密钥：开放 API 侧只认 sd_live_* 前缀
    user_token = client.post(
        PREDICT_PATH,
        headers={"Authorization": f"Bearer {admin_token}"},
        json=_payload(),
    )
    assert user_token.status_code == 401
    assert user_token.json()["code"] == "API_KEY_REQUIRED"


def test_predict_rejects_a_key_without_the_decisions_scope(monkeypatch) -> None:
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["agents:read"])
    seen = _patch_upstream(
        monkeypatch,
        lambda request: httpx.Response(200, json={"answers": {}}, request=request),
    )

    response = client.post(PREDICT_PATH, headers={"Authorization": f"Bearer {key}"}, json=_payload())
    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_SCOPE"
    assert seen == []


def test_decisions_scope_is_granted_by_account_level_keys(monkeypatch) -> None:
    # 账号级全量密钥（AppHeader「API 全量密钥」）覆盖决策能力，员工级密钥不授予
    assert "decisions:run" in USER_FULL_ACCESS_SCOPES
    assert "decisions:run" not in AGENT_RUNTIME_SCOPES


def test_predict_forwards_the_payload_and_returns_laya_answers(monkeypatch) -> None:
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["decisions:run"])

    upstream = {
        "answers": {
            "q_dept": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.99, "technical": 0.01},
                "confidence": 0.98,
            },
            "q_refund": {"type": "noul", "noul": 0.9961, "confidence": 0.9961},
        },
        "routing": {"model": "multilingual"},
        "elapsed_ms": 421.4,
    }
    seen = _patch_upstream(
        monkeypatch,
        lambda request: httpx.Response(200, json=upstream, request=request),
    )

    response = client.post(PREDICT_PATH, headers={"Authorization": f"Bearer {key}"}, json=_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answers"] == upstream["answers"]
    assert body["routing"] == {"model": "multilingual"}
    assert body["elapsed_ms"] == 421.4

    assert len(seen) == 1
    forwarded = seen[0]
    assert forwarded.url.path.endswith("/predict")
    assert json.loads(forwarded.content) == _payload()


def test_predict_validates_the_payload_before_reaching_laya(monkeypatch) -> None:
    client, admin_token = _client(monkeypatch)
    key = _api_key(client, admin_token, ["decisions:run"])
    seen = _patch_upstream(
        monkeypatch,
        lambda request: httpx.Response(200, json={"answers": {}}, request=request),
    )

    response = client.post(
        PREDICT_PATH,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "state": {"background": "  "},
            "questions": {"q_only": {"type": "choice", "instructions": "选？", "criteria": {"one": "一"}}},
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert seen == []
