from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import ApiKeyConsumerGroup, ApiKeyQuotaRule, Tenant, User
from app.api import api_key_applications as apk
from app.aliyun_aigw import config as aigw_config
from app.aliyun_aigw import get_apig_client as real_get_apig_client
from app.api.api_key_applications import (
    ApiKeyApplicationApprove,
    ApiKeyApplicationCreate,
    ApiKeyApplicationReview,
    ApiKeyConsumerGroupCreate,
    ApiKeyConsumerGroupUpdate,
    ApiKeyQuotaRuleCreate,
    ApiKeyQuotaUpdate,
    approve_application,
    create_application,
    create_consumer_group,
    create_quota_rule,
    list_applications,
    list_my_applications,
    list_usage,
    reject_application,
    revoke_application,
    update_consumer_group,
    update_quota,
)


@pytest.fixture(autouse=True)
def _force_aigw_mock(monkeypatch):
    """本文件所有测试强制走 mock 客户端，避免 .env 配置真实 AK/SK 后打到真实阿里云。

    注意 client.py 是 `from .config import USE_MOCK` 拷贝引用，
    必须同时 patch client 模块内的值才能生效。
    """
    from app.aliyun_aigw import client as aigw_client

    monkeypatch.setattr(aigw_config, "USE_MOCK", True)
    monkeypatch.setattr(aigw_client, "USE_MOCK", True)


TENANT = "tenant_test_apk"

GATEWAY_ENV = (
    '[{"name":"主力网关","gateway_id":"gw-test123",'
    '"gateway_url":"https://apigw.example.com/v1","region":"cn-hangzhou"}]'
)

# 让 get_gateway_config 能查到网关(在调用时读取, 故模块级设置即可)
os.environ["ALIYUN_APIG_GATEWAYS"] = GATEWAY_ENV


class FakeApigClient:
    """模拟阿里云 APIG 客户端, 不真正发请求。"""

    def __init__(self):
        self.created_consumers = []
        self.created_rules = []
        self.updated_rules = []
        self.usage_calls = []
        self.attached = []
        self.detached = []
        self.deleted_consumers = []
        self.deleted_rules = []
        self.meta_updates = []
        # 云端资源视图（实时直读接口用）
        self.consumers = {}
        self.quota_rules = {}

    def create_consumer(self, name, api_key=None, description=None, gateway_type="AI"):
        self.created_consumers.append({"name": name, "api_key": api_key})
        return f"consumer-{name}"

    def get_consumer(self, consumer_id):
        return {
            "consumerId": consumer_id,
            "name": consumer_id.replace("consumer-", ""),
            "description": "",
            "gatewayType": "AI",
            "enable": True,
        }

    def list_consumers(self, gateway_type="AI"):
        return list(self.consumers.values())

    def list_quota_rules(self, gateway_id=None):
        return list(self.quota_rules.values())

    def get_quota_rule(self, rule_id, gateway_id=None):
        return self.quota_rules.get(
            rule_id,
            {"ruleId": rule_id, "ruleName": rule_id, "quotaDimension": "credit", "quotaLimit": 0, "periodType": "day"},
        )

    def add_consumer_quota_rule(
        self,
        gateway_id,
        consumer_ids,
        quota_limit,
        period_type,
        rule_name=None,
        timezone="UTC+8",
    ):
        self.created_rules.append(
            {
                "gateway_id": gateway_id,
                "consumer_ids": consumer_ids,
                "quota_limit": quota_limit,
                "period_type": period_type,
            }
        )
        return f"rule-{rule_name or 'x'}"

    def get_consumer_quota_usage(self, gateway_id, rule_id, consumer_id):
        self.usage_calls.append(
            {"gateway_id": gateway_id, "rule_id": rule_id, "consumer_id": consumer_id}
        )
        used = 500 + (hash(consumer_id) % 1500)
        return {"requestId": "fake", "code": "200", "data": {"usedAmount": used}}

    def add_consumer_credential(self, consumer_id, api_key):
        self.attached.append({"consumer_id": consumer_id, "api_key": api_key})

    def attach_consumer_to_rule(self, gateway_id, rule_id, consumer_id):
        self.attached.append({"gateway_id": gateway_id, "rule_id": rule_id, "consumer_id": consumer_id})

    def detach_consumer_from_rule(self, gateway_id, rule_id, consumer_id):
        self.detached.append({"gateway_id": gateway_id, "rule_id": rule_id, "consumer_id": consumer_id})

    def update_consumer_quota_rule(self, gateway_id, rule_id, quota_limit):
        self.updated_rules.append(
            {"gateway_id": gateway_id, "rule_id": rule_id, "quota_limit": quota_limit}
        )

    def update_quota_rule_meta(self, gateway_id, rule_id, rule_name=None, period_type=None):
        self.meta_updates.append(
            {"gateway_id": gateway_id, "rule_id": rule_id, "rule_name": rule_name, "period_type": period_type}
        )

    def delete_consumer(self, consumer_id):
        self.deleted_consumers.append(consumer_id)

    def delete_quota_rule(self, rule_id, gateway_id=None):
        self.deleted_rules.append(rule_id)


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


def _make_group_rule(
    session,
    *,
    gateway_id="gw-test123",
    gateway_name="主力网关",
    consumer_id="consumer-demo",
    rule_id="rule-default",
    quota_limit=100000,
    period_type="month",
):
    """直接构造消费组 + 配额规则(模拟阿里云已创建,带 external id)。"""
    group = ApiKeyConsumerGroup(
        tenant_id=TENANT,
        name="demo-consumer",
        gateway_id=gateway_id,
        gateway_name=gateway_name,
        external_consumer_id=consumer_id,
        status="enabled",
    )
    rule = ApiKeyQuotaRule(
        tenant_id=TENANT,
        name="default-token",
        gateway_id=gateway_id,
        gateway_name=gateway_name,
        quota_dimension="token",
        quota_limit=quota_limit,
        period_type=period_type,
        external_rule_id=rule_id,
        status="enabled",
    )
    session.add(group)
    session.add(rule)
    session.commit()
    session.refresh(group)
    session.refresh(rule)
    return group, rule


def test_apply_and_admin_approve_flow():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)

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

    # 5) admin approves the first application (mock 阿里云)
    pending = admin_list[0]
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            pending.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
                reviewer_note="ok",
            ),
            db=session,
            current_user=admin,
        )
    assert approved.status == "approved"
    assert approved.api_key_masked
    assert approved.api_url == "https://apigw.example.com/v1"
    assert approved.gateway_name == "主力网关"
    assert approved.quota_limit == 100000
    assert approved.quota_period == "month"
    assert approved.consumer_id == "consumer-demo"
    assert approved.consumer_name == "demo-consumer"
    assert approved.consumer_group_name == "demo-consumer"
    assert approved.quota_rule_name == "default-token"

    # 阿里云被调用: 追加凭证 + 纳入配额规则
    assert len(fake.attached) == 2
    assert fake.attached[0]["api_key"].startswith("sk-")
    assert fake.attached[1]["consumer_id"] == "consumer-demo"
    assert fake.attached[1]["rule_id"] == "rule-default"

    # 6) member now sees plaintext key + url on own approved application
    mine_after = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    approved_mine = next(item for item in mine_after if item.status == "approved")
    assert approved_mine.api_key and approved_mine.api_key.startswith("sk-")
    assert approved_mine.api_url == "https://apigw.example.com/v1"

    # 7) approving a non-pending application fails
    already = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            approve_application(
                pending.id,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT, consumer_group_id=group.id, quota_rule_id=rule.id
                ),
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


def test_approve_unknown_group_400():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    fake = FakeApigClient()
    bad = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            approve_application(
                created.id,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT, consumer_group_id="nope", quota_rule_id="nope"
                ),
                db=session,
                current_user=admin,
            )
    except HTTPException as exc:
        bad = exc.status_code == 400
    assert bad, "unknown consumer group should return 400"
    assert not fake.attached, "should not call 阿里云 for unknown group"


def test_approve_mock_mode_success():
    """无 AK/SK 时自动走 mock：审批通过返回 mock 消费者/配额规则 id，并落库网关地址。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    group = ApiKeyConsumerGroup(
        tenant_id=TENANT, name="demo-consumer", gateway_id="gw-test123",
        gateway_name="主力网关", external_consumer_id="cs-mock-001", status="enabled",
    )
    rule = ApiKeyQuotaRule(
        tenant_id=TENANT, name="default-token", gateway_id="gw-test123",
        gateway_name="主力网关", quota_dimension="token", quota_limit=100000,
        period_type="month", external_rule_id="qr-mock-001", status="enabled",
    )
    session.add(group)
    session.add(rule)
    session.commit()
    session.refresh(group)
    session.refresh(rule)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    # 不 patch：用 aliyun_aigw 真实 mock 分支（approve 传云端 consumerId/ruleId）
    approved = approve_application(
        created.id,
        ApiKeyApplicationApprove(
            tenant_id=TENANT,
            consumer_group_id="cs-mock-001",
            quota_rule_id="qr-mock-001",
        ),
        db=session,
        current_user=admin,
    )
    assert approved.status == "approved"
    assert approved.api_url == "https://apigw.example.com/v1"
    assert approved.consumer_id == "cs-mock-001"
    assert approved.quota_rule_id == "qr-mock-001"
    # 实时架构下 consumer_group_name 取云端消费者名（mock 分支无该消费者时回退 ID）
    assert approved.consumer_group_name


def test_get_apig_client_none_in_production_without_ak(monkeypatch):
    """生产模式(USE_MOCK=False)且缺 AK/SK 时，get_apig_client 返回 None。"""
    monkeypatch.setattr(aigw_config, "USE_MOCK", False)
    monkeypatch.setattr(aigw_config, "ACCESS_KEY_ID", "")
    monkeypatch.setattr(aigw_config, "ACCESS_KEY_SECRET", "")
    assert real_get_apig_client() is None


def test_approve_group_rule_gateway_mismatch_400():
    """approve 传入云端不存在的配额规则 ID 时应返回 400（无效引用校验）。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    group, rule = _make_group_rule(session)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    fake = FakeApigClient()
    bad = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            approve_application(
                created.id,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT,
                    consumer_group_id="cs-missing",
                    quota_rule_id="qr-missing",
                ),
                db=session,
                current_user=admin,
            )
    except HTTPException as exc:
        bad = exc.status_code == 400
    assert bad, "invalid consumer/rule reference should return 400"


def test_revoke_clears_key():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT, consumer_group_id=group.id, quota_rule_id=rule.id
            ),
            db=session,
            current_user=admin,
        )
    revoked = revoke_application(
        created.id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="回收"),
        db=session,
        current_user=admin,
    )
    assert revoked.status == "revoked"
    assert revoked.api_key is None
    assert revoked.api_url is None


def test_non_admin_cannot_list_all():
    session = _make_session()
    member = _member(session)
    blocked = False
    try:
        list_applications(tenant_id=TENANT, db=session, current_user=member)
    except HTTPException as exc:
        blocked = exc.status_code == 403
    assert blocked


def test_usage_admin_only():
    session = _make_session()
    member = _member(session)
    blocked = False
    try:
        list_usage(tenant_id=TENANT, db=session, current_user=member)
    except HTTPException as exc:
        blocked = exc.status_code == 403
    assert blocked


def test_usage_summary_and_items():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=2000, period_type="day")

    apps = []
    for purpose in ("业务 A", "业务 B"):
        created = create_application(
            ApiKeyApplicationCreate(tenant_id=TENANT, purpose=purpose),
            db=session,
            current_user=member,
        )
        with patch.object(apk, "get_apig_client", return_value=fake):
            approved = approve_application(
                created.id,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT, consumer_group_id=group.id, quota_rule_id=rule.id
                ),
                db=session,
                current_user=admin,
            )
        apps.append(approved)

    with patch.object(apk, "get_apig_client", return_value=fake):
        usage = list_usage(tenant_id=TENANT, db=session, current_user=admin)

    assert usage.month
    assert usage.summary.allocated_users == 2
    assert usage.summary.total_quota == 4000
    assert usage.summary.total_used > 0
    assert 0.0 <= usage.summary.avg_usage_rate <= 1.0
    assert len(usage.items) == 2
    assert usage.items[0].quota_limit == 2000
    assert usage.items[0].used_amount > 0
    assert usage.items[0].usage_rate > 0
    assert usage.items[0].suggestion in ("expand", "watch", "normal")
    assert len(fake.usage_calls) == 2


def test_update_quota_admin_and_flow():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=1000)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT, consumer_group_id=group.id, quota_rule_id=rule.id
            ),
            db=session,
            current_user=admin,
        )

    # 普通成员不能调整配额
    blocked = False
    try:
        update_quota(
            created.id,
            ApiKeyQuotaUpdate(tenant_id=TENANT, quota_limit=2000),
            db=session,
            current_user=member,
        )
    except HTTPException as exc:
        blocked = exc.status_code == 403
    assert blocked

    # 管理员调整配额
    with patch.object(apk, "get_apig_client", return_value=fake):
        updated = update_quota(
            created.id,
            ApiKeyQuotaUpdate(tenant_id=TENANT, quota_limit=2000),
            db=session,
            current_user=admin,
        )
    assert updated.quota_limit == 2000
    assert len(fake.updated_rules) == 1
    assert fake.updated_rules[0]["quota_limit"] == 2000


def test_update_quota_validation():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=1000)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )

    # 未批准的申请不能调整配额
    bad = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            update_quota(
                created.id,
                ApiKeyQuotaUpdate(tenant_id=TENANT, quota_limit=2000),
                db=session,
                current_user=admin,
            )
    except HTTPException as exc:
        bad = exc.status_code == 409
    assert bad

    # 非法配额值
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT, consumer_group_id=group.id, quota_rule_id=rule.id
            ),
            db=session,
            current_user=admin,
        )
    bad = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            update_quota(
                created.id,
                ApiKeyQuotaUpdate(tenant_id=TENANT, quota_limit=0),
                db=session,
                current_user=admin,
            )
    except HTTPException as exc:
        bad = exc.status_code == 400
    assert bad


def test_create_consumer_group_and_quota_rule():
    """消费组与配额规则创建接口走 mock 客户端,落库 external id。"""
    session = _make_session()
    admin = _admin(session)
    group = create_consumer_group(
        ApiKeyConsumerGroupCreate(
            tenant_id=TENANT, name="demo-consumer", description="演示", gateway_name="主力网关"
        ),
        db=session,
        current_user=admin,
    )
    assert group.external_consumer_id and group.external_consumer_id.startswith("cs-mock-")
    assert group.gateway_name == "主力网关"

    rule = create_quota_rule(
        ApiKeyQuotaRuleCreate(
            tenant_id=TENANT, name="default-token", gateway_name="主力网关",
            quota_dimension="token", quota_limit=1000, period_type="day",
        ),
        db=session,
        current_user=admin,
    )
    assert rule.external_rule_id and rule.external_rule_id.startswith("qr-mock-")
    assert rule.quota_limit == 1000
    assert rule.period_type == "day"


def test_create_consumer_group_with_owner():
    """消费组创建支持业务「归属」字段（非阿里云字段），落库并回读。"""
    session = _make_session()
    admin = _admin(session)
    group = create_consumer_group(
        ApiKeyConsumerGroupCreate(
            tenant_id=TENANT,
            name="owner-demo",
            description="归属演示",
            owner="重庆项目",
            gateway_name="主力网关",
        ),
        db=session,
        current_user=admin,
    )
    assert group.owner == "重庆项目"


def test_consumer_group_owners_from_config():
    """归属下拉选项来自 CONSUMER_GROUP_OWNERS 配置（默认内置列表）。"""
    from app.config import get_settings

    owners = get_settings().consumer_group_owner_list
    assert "重庆项目" in owners
    assert "总部IT" in owners  # 品牌演进：默认列表已由「复星总部IT」改为「总部IT」
    assert "Club Med" in owners


def test_update_consumer_group():
    """消费组编辑:名称/描述/归属更新落库,描述变化时同步阿里云(mock)。"""
    session = _make_session()
    admin = _admin(session)
    group = create_consumer_group(
        ApiKeyConsumerGroupCreate(
            tenant_id=TENANT,
            name="edit-demo",
            description="旧描述",
            owner="重庆项目",
            gateway_name="主力网关",
        ),
        db=session,
        current_user=admin,
    )
    assert group.external_consumer_id  # mock 客户端已创建消费者

    updated = update_consumer_group(
        group.external_consumer_id,  # 实时架构：路径参数即云端 consumerId
        ApiKeyConsumerGroupUpdate(
            tenant_id=TENANT,
            name="edit-demo-renamed",
            description="新描述",
            owner="Club Med",
        ),
        db=session,
        current_user=admin,
    )
    assert updated.name == "edit-demo-renamed"
    assert updated.description == "新描述"
    assert updated.owner == "Club Med"

    # 回读确认已落库
    row = session.get(ApiKeyConsumerGroup, group.id)
    assert row is not None
    assert row.name == "edit-demo-renamed"
    assert row.description == "新描述"
    assert row.owner == "Club Med"


def test_update_consumer_group_empty_name_rejected():
    """编辑消费组时名称为空 → 400。"""
    session = _make_session()
    admin = _admin(session)
    group = create_consumer_group(
        ApiKeyConsumerGroupCreate(
            tenant_id=TENANT,
            name="blank-name-demo",
            gateway_name="主力网关",
        ),
        db=session,
        current_user=admin,
    )
    bad = False
    try:
        update_consumer_group(
            group.id,
            ApiKeyConsumerGroupUpdate(tenant_id=TENANT, name="   "),
            db=session,
            current_user=admin,
        )
    except HTTPException as exc:
        bad = exc.status_code == 400
    assert bad
