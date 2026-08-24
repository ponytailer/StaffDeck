from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import Tenant, User
from app.api.api_key_applications import (
    ApiKeyApplicationCreate,
    ApiKeyApplicationReview,
    approve_application,
    create_application,
    list_applications,
    list_my_applications,
    reject_application,
)

TENANT = "tenant_test_apk"


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=TENANT, name="test"))
    session.add(
        User(id="u_admin", tenant_id=TENANT, username="admin", role="admin", password_hash="x")
    )
    session.add(
        User(id="u_member", tenant_id=TENANT, username="member", role="member", password_hash="x")
    )
    session.commit()
    return session


def _admin(session):
    return session.get(User, "u_admin")


def _member(session):
    return session.get(User, "u_member")


def test_apply_and_admin_approve_flow():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)

    # 1) member applies for an API key
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="对接业务系统"),
        db=session,
        current_user=member,
    )
    assert created.status == "pending"
    assert created.username == "member"

    # 2) max 2 active per user enforced
    create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="第二个"),
        db=session,
        current_user=member,
    )
    hit_limit = False
    try:
        create_application(
            ApiKeyApplicationCreate(tenant_id=TENANT, purpose="超额"),
            db=session,
            current_user=member,
        )
    except HTTPException as exc:
        hit_limit = exc.status_code == 409
    assert hit_limit, "third application should be rejected with 409"

    # 3) member sees own applications, no key while pending
    mine = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    assert len(mine) == 2
    assert all(item.api_key is None for item in mine)

    # 4) admin list shows pending first, never plaintext key
    admin_list = list_applications(tenant_id=TENANT, db=session, current_user=admin)
    assert admin_list[0].status == "pending"
    assert admin_list[0].api_key is None

    # 5) admin approves the first application -> key + url assigned
    pending = admin_list[0]
    approved = approve_application(
        pending.id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="ok"),
        db=session,
        current_user=admin,
    )
    assert approved.status == "approved"
    assert approved.api_key_masked
    assert approved.api_url and approved.api_url.startswith("https://")

    # 6) member now sees plaintext key + url on own approved application
    mine_after = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    approved_mine = next(item for item in mine_after if item.status == "approved")
    assert approved_mine.api_key and approved_mine.api_key.startswith("sk-fosun-")
    assert approved_mine.api_url

    # 7) approving a non-pending application fails
    already = False
    try:
        approve_application(
            pending.id,
            ApiKeyApplicationReview(tenant_id=TENANT),
            db=session,
            current_user=admin,
        )
    except HTTPException as exc:
        already = exc.status_code == 409
    assert already

    # 8) rejection flow on the remaining pending
    remaining = [item for item in admin_list if item.status == "pending" and item.id != pending.id]
    assert remaining
    rejected = reject_application(
        remaining[0].id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="用途不明确"),
        db=session,
        current_user=admin,
    )
    assert rejected.status == "rejected"
    assert rejected.reviewer_note == "用途不明确"


def test_non_admin_cannot_list_all():
    session = _make_session()
    member = _member(session)
    blocked = False
    try:
        list_applications(tenant_id=TENANT, db=session, current_user=member)
    except HTTPException as exc:
        blocked = exc.status_code == 403
    assert blocked
