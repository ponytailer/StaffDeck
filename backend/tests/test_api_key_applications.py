from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select

from app.db.models import ApiKeyApplication, ApiKeyConsumer, ApiKeyConsumerGroup, ApiKeyQuotaRule, Tenant, User
from app.api import api_key_applications as apk
from app.aliyun_aigw import config as aigw_config
from app.aliyun_aigw import get_apig_client as real_get_apig_client
from app.api.api_key_applications import (
    ApiKeyApplicationApprove,
    ApiKeyApplicationCreate,
    ApiKeyApplicationReview,
    ApiKeyConsumerGroupCreate,
    ApiKeyQuotaRuleCreate,
    ApiKeyQuotaUpdate,
    approval_stats,
    approve_application,
    create_application,
    create_quota_rule,
    delete_my_application,
    list_applications,
    list_my_applications,
    list_my_usage,
    list_usage,
    reject_application,
    revoke_application,
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
        self.attached_groups = []
        self.detached = []
        self.deleted_consumers = []
        self.deleted_rules = []
        self.meta_updates = []
        self.group_adds = []
        self.authz_rules = []
        self.known_groups = {}
        # 云端资源视图（实时直读接口用）
        self.consumers = {}
        self.quota_rules = {}
        # 云端消费组视图（ListConsumerGroups）+ 消费者组归属（GetConsumer.consumerGroups）
        self.cloud_groups = {}
        self.consumer_group_members = {}
        # 组主体用量（subjectType=consumer_group 主体行回带的 usedAmount）
        self.group_usage = {}

    def create_consumer(self, name, api_key=None, description=None, gateway_type="AI"):
        self.created_consumers.append({"name": name, "api_key": api_key})
        return f"consumer-{len(self.created_consumers):03d}"

    def get_consumer(self, consumer_id):
        return {
            "consumerId": consumer_id,
            "name": consumer_id.replace("consumer-", ""),
            "description": "",
            "gatewayType": "AI",
            "enable": True,
            "consumerGroups": list(self.consumer_group_members.get(consumer_id, [])),
        }

    def list_consumers(self, gateway_type="AI"):
        return list(self.consumers.values())

    def list_consumer_groups(self, gateway_type="AI"):
        return list(self.cloud_groups.values())

    def list_quota_rules(self, gateway_id=None):
        return list(self.quota_rules.values())

    def list_quota_rule_subjects(self, rule_id, gateway_id=None):
        seen = []
        for a in self.attached:
            cid = a.get("consumer_id")
            if cid and cid not in seen:
                seen.append(cid)
        items = [{"id": cid, "usedAmount": 500} for cid in seen]
        # 组粒度规则视图：attached_groups 里的组以 subjectType=consumer_group 返回
        seen_groups = []
        for a in self.attached_groups:
            gid = a.get("consumer_group_id")
            if gid and gid not in seen_groups:
                seen_groups.append(gid)
        items.extend(
            {"id": gid, "subjectType": "consumer_group", "usedAmount": self.group_usage.get(gid, 0)}
            for gid in seen_groups
        )
        return items

    def attach_consumer_group_to_rule(self, gateway_id, rule_id, consumer_group_id):
        self.attached_groups.append(
            {"gateway_id": gateway_id, "rule_id": rule_id, "consumer_group_id": consumer_group_id}
        )

    def detach_consumer_group_from_rule(self, gateway_id, rule_id, consumer_group_id):
        self.attached_groups = [
            a for a in self.attached_groups if a.get("consumer_group_id") != consumer_group_id
        ]

    def get_quota_rule(self, rule_id, gateway_id=None):
        rule = self.quota_rules.get(rule_id)
        if rule is None:
            raise RuntimeError(f"quota rule {rule_id} not found")
        return rule

    def get_consumer_group(self, group_id):
        info = self.known_groups.get(group_id)
        if info is None:
            raise RuntimeError(f"consumer group {group_id} not found")
        return info

    def add_consumers_to_group(self, group_id, consumer_ids):
        self.group_adds.append({"group_id": group_id, "consumer_ids": list(consumer_ids)})
        for cid in consumer_ids:
            members = self.consumer_group_members.setdefault(cid, [])
            if not any(g.get("consumerGroupId") == group_id for g in members):
                members.append({"consumerGroupId": group_id, "name": group_id})

    def ensure_group_member_authorized(self, consumer_id, group_id, gateway_id, api_id=None):
        self.authz_rules.append(
            {"consumer_id": consumer_id, "group_id": group_id, "gateway_id": gateway_id}
        )
        return None

    def add_consumer_quota_rule(
        self,
        gateway_id,
        consumer_ids,
        quota_limit,
        period_type,
        rule_name=None,
        timezone="UTC+8",
        quota_dimension="credit",
        consumer_group_ids=None,
    ):
        self.created_rules.append(
            {
                "gateway_id": gateway_id,
                "consumer_ids": consumer_ids,
                "consumer_group_ids": consumer_group_ids,
                "quota_dimension": quota_dimension,
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
        # 模拟云端行为：额度更新写回规则视图（后续 get_quota_rule 可见）
        if rule_id in self.quota_rules:
            self.quota_rules[rule_id]["quotaLimit"] = quota_limit

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


def _register_cloud_refs(fake, group, rule):
    """把本地消费组/配额规则注册进 fake 云端视图（get_consumer_group/get_quota_rule 可查）。"""
    fake.known_groups[group.id] = {"consumerGroupId": group.id, "name": group.name}
    fake.quota_rules[rule.id] = {
        "ruleId": rule.id,
        "ruleName": rule.name,
        "quotaDimension": rule.quota_dimension,
        "quotaLimit": rule.quota_limit,
        "periodType": rule.period_type,
    }


def test_apply_and_admin_approve_flow():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)

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
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
                reviewer_note="ok",
            ),
            db=session,
            current_user=admin,
        )
    assert approved.status == "approved"
    assert approved.api_key_masked
    assert approved.api_url == "https://ai-gateway.folidaymall.com/v1/chat/completions"
    assert approved.gateway_name == "主力网关"
    assert approved.quota_limit == 100000
    assert approved.quota_period == "month"
    assert approved.consumer_id == "consumer-001"
    assert approved.consumer_name == "demo-consumer"
    assert approved.consumer_group_name == "demo-consumer"
    assert approved.quota_rule_name == "default-token"

    # 阿里云被调用: 创建消费者(服务端生成 Key) + 入组 + 绑配额 + 授权规则
    assert len(fake.created_consumers) == 1
    assert fake.created_consumers[0]["api_key"].startswith("sk-")
    assert fake.group_adds == [{"group_id": group.id, "consumer_ids": ["consumer-001"]}]
    assert len(fake.attached) == 1
    assert fake.attached[0]["consumer_id"] == "consumer-001"
    assert fake.attached[0]["rule_id"] == rule.id
    assert len(fake.authz_rules) == 1
    assert fake.authz_rules[0]["consumer_id"] == "consumer-001"
    assert fake.authz_rules[0]["group_id"] == group.id

    # 6) member now sees plaintext key + url on own approved application
    mine_after = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    approved_mine = next(item for item in mine_after if item.status == "approved")
    assert approved_mine.api_key and approved_mine.api_key.startswith("sk-")
    assert approved_mine.api_url == "https://ai-gateway.folidaymall.com/v1/chat/completions"

    # 7) approving a non-pending application fails
    already = False
    try:
        with patch.object(apk, "get_apig_client", return_value=fake):
            approve_application(
                pending.id,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT,
                    consumer_name="demo-consumer",
                    consumer_group_id=group.id,
                    quota_rule_id=rule.id,
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


def test_approval_stats_history_includes_approved():
    """审核历史口径 = 所有已处理（非 pending）的申请，含已批准。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)

    # 两个申请：一个批准、一个驳回
    approved_app = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="批准用"),
        db=session,
        current_user=member,
    )
    rejected_app = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="驳回用"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            approved_app.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )
    reject_application(
        rejected_app.id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="不通过"),
        db=session,
        current_user=admin,
    )

    stats = approval_stats(tenant_id=TENANT, db=session, current_user=admin)
    assert stats.pending == 0
    assert stats.allocated == 1
    assert stats.history == 2  # 已批准 + 已驳回都计入审核历史


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
                    tenant_id=TENANT,
                    consumer_name="demo-consumer",
                    consumer_group_id="nope",
                    quota_rule_id="nope",
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
            consumer_name="demo-consumer",
            consumer_group_id="cs-mock-001",
            quota_rule_id="qr-mock-001",
        ),
        db=session,
        current_user=admin,
    )
    assert approved.status == "approved"
    assert approved.api_url == "https://ai-gateway.folidaymall.com/v1/chat/completions"
    # 新流程：审批创建全新消费者（mock 分支返回 cs-mock-xxx 新 ID）
    assert approved.consumer_id.startswith("cs-mock-")
    assert approved.consumer_name == "demo-consumer"
    assert approved.quota_rule_id == "qr-mock-001"
    assert approved.consumer_group_name  # mock GetConsumerGroup 分支返回组名
    assert approved.quota_limit == 1000  # mock qr-mock-001 的限额
    assert approved.quota_period == "day"


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
                    consumer_name="demo-consumer",
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
    _register_cloud_refs(fake, group, rule)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
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


def test_revoke_when_consumer_deleted_on_cloud():
    """云端消费者已删除时吊销：跳过云端调用，本地正常落库为 revoked。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )

    # 模拟云端消费者已被管理员手动删除：GetConsumer 抛错
    def _raise(cid):
        raise RuntimeError("consumer not found")

    with patch.object(fake, "get_consumer", side_effect=_raise):
        revoked = revoke_application(
            created.id,
            ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="云端已删除，清理记录"),
            db=session,
            current_user=admin,
        )
    assert revoked.status == "revoked"
    assert revoked.api_key is None
    assert revoked.api_url is None
    # 未尝试云端更新
    assert fake.detached == []


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
    _register_cloud_refs(fake, group, rule)

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
                    tenant_id=TENANT,
                    consumer_name="demo-consumer",
                    consumer_group_id=group.id,
                    quota_rule_id=rule.id,
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
    # 新链路：用量经 list_quota_rule_subjects 一次性返回，不再逐消费者调 usage


def test_usage_current_month_aligns_with_consumers():
    """当月用量主体与消费者列表对齐：快照中已删除的消费者不再展示（历史月仍保留）。"""
    from datetime import datetime, timezone

    from app.db.models import ApiKeyUsageSnapshot

    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=2000, period_type="month")
    _register_cloud_refs(fake, group, rule)

    # 本地仅 1 个在册消费者
    session.add(
        ApiKeyConsumer(
            tenant_id=TENANT,
            name="live-consumer",
            gateway_id="gw-test123",
            gateway_name="主力网关",
            external_consumer_id="cs-live",
            status="enabled",
            enable=True,
        )
    )
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    # 当月快照含一个已在云端/本地删除的测试消费者
    session.add(
        ApiKeyUsageSnapshot(
            tenant_id=TENANT,
            month=month,
            consumer_id="cs-removed-test",
            consumer_name="testprobe",
            gateway_id="gw-test123",
            gateway_name="主力网关",
            quota_rule_id=rule.external_rule_id or "",
            quota_rule_name=rule.name,
            quota_limit=2000,
            quota_period="month",
            used_amount=123,
        )
    )
    # 历史月快照（同已删消费者）应保留
    session.add(
        ApiKeyUsageSnapshot(
            tenant_id=TENANT,
            month="2020-01",
            consumer_id="cs-removed-test",
            consumer_name="testprobe",
            gateway_id="gw-test123",
            gateway_name="主力网关",
            quota_rule_id=rule.external_rule_id or "",
            quota_rule_name=rule.name,
            quota_limit=2000,
            quota_period="month",
            used_amount=456,
        )
    )
    session.commit()

    with patch.object(apk, "get_apig_client", return_value=fake):
        current = list_usage(tenant_id=TENANT, db=session, current_user=admin)
        history = list_usage(tenant_id=TENANT, month="2020-01", db=session, current_user=admin)

    # 当月：只显示在册消费者，已删测试消费者不出现，汇总不含其用量
    assert [item.consumer_id for item in current.items] == ["cs-live"]
    assert current.summary.allocated_users == 1
    assert current.summary.total_used == 0
    # 历史月：主体 = 当前消费者 ∪ 快照消费者（已删消费者保留）
    assert [item.consumer_id for item in history.items] == ["cs-live", "cs-removed-test"]
    assert history.items[1].used_amount == 456


def test_mine_returns_consumer_status():
    """/mine 回带关联消费者的启停状态：停用消费者对应申请返回 disabled。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=2000, period_type="month")
    _register_cloud_refs(fake, group, rule)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )

    # 审批后正常：consumer_status 为 enabled（来自本地消费者行）
    mine = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    target = next(r for r in mine if r.id == approved.id)
    assert target.consumer_status == "enabled"

    # 消费者被停用后：consumer_status 变为 disabled
    consumer_row = session.exec(
        select(ApiKeyConsumer).where(ApiKeyConsumer.external_consumer_id.is_not(None))
    ).first()
    assert consumer_row is not None
    consumer_row.status = "disabled"
    consumer_row.enable = False
    session.add(consumer_row)
    session.commit()

    mine = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    target = next(r for r in mine if r.id == approved.id)
    assert target.consumer_status == "disabled"


def test_mine_returns_consumer_deleted_after_row_removed():
    """消费者行被删除（云端删除后 mirror 同步）时，approved 申请回带 deleted。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=2000, period_type="month")
    _register_cloud_refs(fake, group, rule)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )

    # 模拟 mirror 同步：云端消费者已删除 → 本地行被移除
    consumer_row = session.exec(
        select(ApiKeyConsumer).where(ApiKeyConsumer.external_consumer_id.is_not(None))
    ).first()
    assert consumer_row is not None
    session.delete(consumer_row)
    session.commit()

    # /mine：approved 申请回带 deleted
    mine = list_my_applications(tenant_id=TENANT, db=session, current_user=member)
    target = next(r for r in mine if r.id == approved.id)
    assert target.status == "approved"
    assert target.consumer_status == "deleted"

    # 管理员列表同样回带 deleted
    rows = list_applications(tenant_id=TENANT, db=session, current_user=admin)
    target_admin = next(r for r in rows if r.id == approved.id)
    assert target_admin.consumer_status == "deleted"


def test_delete_my_application():
    """用户删除自己名下终止态记录：rejected/revoked/消费者已删除的 approved 可删，pending/有效 approved 不可删。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)

    # 场景1：pending 不可删
    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="p1"),
        db=session,
        current_user=member,
    )
    blocked = False
    try:
        delete_my_application(
            created.id, tenant_id=TENANT, db=session, current_user=member
        )
    except HTTPException as exc:
        blocked = exc.status_code == 409
    assert blocked

    # 场景2：有效 approved（消费者仍存在）不可删
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )
    blocked = False
    try:
        delete_my_application(
            approved.id, tenant_id=TENANT, db=session, current_user=member
        )
    except HTTPException as exc:
        blocked = exc.status_code == 409
    assert blocked

    # 场景3：revoked 可删
    revoked = revoke_application(
        approved.id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="回收"),
        db=session,
        current_user=admin,
    )
    delete_my_application(revoked.id, tenant_id=TENANT, db=session, current_user=member)
    assert session.get(ApiKeyApplication, revoked.id) is None

    # 场景4：rejected 可删
    created2 = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="p2"),
        db=session,
        current_user=member,
    )
    rejected = reject_application(
        created2.id,
        ApiKeyApplicationReview(tenant_id=TENANT, reviewer_note="不合规"),
        db=session,
        current_user=admin,
    )
    delete_my_application(rejected.id, tenant_id=TENANT, db=session, current_user=member)
    assert session.get(ApiKeyApplication, rejected.id) is None

    # 场景5：approved 且消费者已被删除（deleted）→ 可删
    created3 = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="p3"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved3 = approve_application(
            created3.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer-2",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )
    consumer_row = session.exec(
        select(ApiKeyConsumer).where(
            ApiKeyConsumer.external_consumer_id == approved3.consumer_id
        )
    ).first()
    assert consumer_row is not None
    session.delete(consumer_row)
    session.commit()
    delete_my_application(approved3.id, tenant_id=TENANT, db=session, current_user=member)
    assert session.get(ApiKeyApplication, approved3.id) is None


def test_approve_skips_consumer_attach_for_group_scoped_rule():
    """审批时目标规则为组粒度（subjects 含 consumer_group）：跳过逐消费者 addIds。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)
    # 预先把组绑到规则 → 规则变成组粒度
    fake.attach_consumer_group_to_rule("gw-test123", rule.id, group.id)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="grp"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="grp-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )
    assert approved.status == "approved"
    # 组粒度规则：消费者不应被逐个 addIds
    assert fake.attached == []


def test_approve_attaches_consumer_for_consumer_scoped_rule():
    """审批时目标规则为消费者粒度（无 consumer_group 主体）：维持逐消费者 addIds。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="csm"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="csm-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
            ),
            db=session,
            current_user=admin,
        )
    assert approved.status == "approved"
    # 消费者粒度规则：新消费者被 addIds 绑定
    assert len(fake.attached) == 1
    assert fake.attached[0]["consumer_id"] == approved.consumer_id


def test_create_quota_rule_group_mode():
    """创建配额规则支持 consumer_group 主体类型（透传 consumerGroupIds）。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, _rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, _rule)

    payload = ApiKeyQuotaRuleCreate(
        tenant_id=TENANT,
        name="group-quota",
        gateway_name="主力网关",
        quota_dimension="credit",
        quota_limit=5000,
        period_type="month",
        subject_type="consumer_group",
        consumer_group_ids=[group.id],
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        created = create_quota_rule(payload, db=session, current_user=admin)
    assert created.external_rule_id
    # fake 收到组粒度创建调用
    assert fake.created_rules[-1].get("consumer_group_ids") == [group.id]


def test_create_quota_rule_rejects_group_mode_without_groups():
    """subject_type=consumer_group 但未传组 ID → 400。"""
    session = _make_session()
    admin = _admin(session)
    group, _rule = _make_group_rule(session)
    payload = ApiKeyQuotaRuleCreate(
        tenant_id=TENANT,
        name="bad-group-quota",
        gateway_name="主力网关",
        quota_limit=1000,
        period_type="month",
        subject_type="consumer_group",
        consumer_group_ids=[],
    )
    blocked = False
    try:
        create_quota_rule(payload, db=session, current_user=admin)
    except HTTPException as exc:
        blocked = exc.status_code == 400
    assert blocked


def test_create_limit_ignores_deleted_consumer():
    """上限口径：approved 但消费者已被删除的记录不占申请名额。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, rule)

    # 造满 MAX_APPLICATIONS_PER_USER 个 approved
    created_ids = []
    for i in range(apk.MAX_APPLICATIONS_PER_USER):
        c = create_application(
            ApiKeyApplicationCreate(tenant_id=TENANT, purpose=f"limit-{i}"),
            db=session,
            current_user=member,
        )
        created_ids.append(c.id)
    with patch.object(apk, "get_apig_client", return_value=fake):
        for cid in created_ids:
            approve_application(
                cid,
                ApiKeyApplicationApprove(
                    tenant_id=TENANT,
                    consumer_name=f"limit-consumer-{cid[-4:]}",
                    consumer_group_id=group.id,
                    quota_rule_id=rule.id,
                ),
                db=session,
                current_user=admin,
            )

    # 已达上限 → 409
    blocked = False
    try:
        create_application(
            ApiKeyApplicationCreate(tenant_id=TENANT, purpose="over"),
            db=session,
            current_user=member,
        )
    except HTTPException as exc:
        blocked = exc.status_code == 409
    assert blocked

    # 删掉一个消费者的本地行（模拟云端删除+同步）→ 名额释放，可再申请
    consumer_row = session.exec(
        select(ApiKeyConsumer).where(
            ApiKeyConsumer.external_consumer_id.is_not(None)
        )
    ).first()
    session.delete(consumer_row)
    session.commit()

    row = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="after-release"),
        db=session,
        current_user=member,
    )
    assert row.status == "pending"


def test_update_quota_admin_and_flow():
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=1000)
    _register_cloud_refs(fake, group, rule)

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="x"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
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
    _register_cloud_refs(fake, group, rule)

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
                tenant_id=TENANT,
                consumer_name="demo-consumer",
                consumer_group_id=group.id,
                quota_rule_id=rule.id,
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




def test_consumer_group_owners_from_config():
    """归属下拉选项来自 CONSUMER_GROUP_OWNERS 配置（默认内置列表）。"""
    from app.config import get_settings

    owners = get_settings().consumer_group_owner_list
    assert "重庆项目" in owners
    assert "总部IT" in owners  # 品牌演进：默认列表已由「复星总部IT」改为「总部IT」
    assert "Club Med" in owners


def test_sync_from_aliyun_mirror_semantics():
    """手动同步：云端为准镜像同步消费组/消费者/配额规则（含移除云端已删项、归属清空）。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()

    # 云端视图：1 组 + 2 消费者（probe 已入组 / free 已被移出组）+ 1 配额规则
    fake.cloud_groups["csg-new"] = {
        "consumerGroupId": "csg-new",
        "name": "AILab",
        "consumerCount": 1,
    }
    fake.consumers["cs-new"] = {
        "consumerId": "cs-new",
        "name": "probe",
        "description": "",
        "gatewayType": "AI",
        "enable": True,
        "deployStatus": "Deployed",
    }
    fake.consumer_group_members["cs-new"] = [{"consumerGroupId": "csg-new", "name": "AILab"}]
    fake.consumers["cs-free"] = {
        "consumerId": "cs-free",
        "name": "free",
        "description": "",
        "gatewayType": "AI",
        "enable": True,
    }
    fake.quota_rules["qr-new"] = {
        "ruleId": "qr-new",
        "ruleName": "AILab_Credits",
        "quotaDimension": "credit",
        "quotaLimit": 6000,
        "periodType": "month",
        "ruleStatus": "enabled",
    }

    # 本地预置：过期组（云端已删）、纯本地组（无 external id）、
    # 过期消费者（云端已删）、归属过期的消费者（云端已移出组）、过期规则
    session.add(
        ApiKeyConsumerGroup(
            tenant_id=TENANT, name="stale-group", gateway_id="gw-test123",
            gateway_name="主力网关", external_consumer_group_id="csg-old",
        )
    )
    session.add(
        ApiKeyConsumerGroup(
            tenant_id=TENANT, name="manual-group", gateway_id="gw-test123", gateway_name="主力网关"
        )
    )
    session.add(
        ApiKeyConsumer(
            tenant_id=TENANT, name="dead-consumer", gateway_id="gw-test123",
            gateway_name="主力网关", external_consumer_id="cs-old",
        )
    )
    session.add(
        ApiKeyConsumer(
            tenant_id=TENANT, name="free", gateway_id="gw-test123",
            gateway_name="主力网关", external_consumer_id="cs-free",
            external_consumer_group_id="csg-old", consumer_group_name="oldgroup",
        )
    )
    session.add(
        ApiKeyQuotaRule(
            tenant_id=TENANT, name="stale-rule", gateway_id="gw-test123",
            gateway_name="主力网关", quota_dimension="token", quota_limit=100,
            period_type="month", external_rule_id="qr-old",
        )
    )
    session.commit()

    with patch.object(apk, "get_apig_client", return_value=fake):
        result = apk.sync_consumer_groups_from_aliyun(
            apk.ApiKeyAliyunSyncRequest(tenant_id=TENANT), db=session, current_user=admin
        )

    assert result.synced_at
    assert result.groups == {"created": 1, "updated": 0, "removed": 1}
    assert result.consumers == {"created": 1, "updated": 1, "removed": 1}
    assert result.quota_rules == {"created": 1, "updated": 0, "removed": 1}

    # 组：云端新增 AILab、云端已删的 stale-group 移除、纯本地 manual-group 保留
    groups = session.exec(
        select(ApiKeyConsumerGroup).where(ApiKeyConsumerGroup.tenant_id == TENANT)
    ).all()
    by_name = {g.name: g for g in groups}
    assert "stale-group" not in by_name
    assert "manual-group" in by_name
    assert by_name["AILab"].external_consumer_group_id == "csg-new"

    # 消费者：probe 新增并带云端归属；free 归属被云端清空；dead-consumer 移除
    consumers = session.exec(
        select(ApiKeyConsumer).where(ApiKeyConsumer.tenant_id == TENANT)
    ).all()
    by_cname = {c.name: c for c in consumers}
    assert set(by_cname) == {"probe", "free"}
    assert by_cname["probe"].external_consumer_id == "cs-new"
    assert by_cname["probe"].external_consumer_group_id == "csg-new"
    assert by_cname["probe"].consumer_group_name == "AILab"
    assert by_cname["free"].external_consumer_group_id is None
    assert by_cname["free"].consumer_group_name is None

    # 配额规则：云端新增 AILab_Credits、过期 stale-rule 移除
    rules = session.exec(
        select(ApiKeyQuotaRule).where(ApiKeyQuotaRule.tenant_id == TENANT)
    ).all()
    assert [r.name for r in rules] == ["AILab_Credits"]
    assert int(rules[0].quota_limit) == 6000


def test_sync_from_aliyun_requires_admin():
    session = _make_session()
    member = _member(session)
    with pytest.raises(HTTPException):
        apk.sync_consumer_groups_from_aliyun(
            apk.ApiKeyAliyunSyncRequest(tenant_id=TENANT), db=session, current_user=member
        )





def _register_group_scoped_rule(session, fake, group, rule, *, group_used=777):
    """把规则注册为组粒度：get_quota_rule 回带 subjectType，组作为主体带用量。"""
    # 生产中组行持有云端组 ID（csg-），主体回填组名依赖该映射；本地 id 与云端 id 对齐
    if not group.external_consumer_group_id:
        group.external_consumer_group_id = group.id
        session.add(group)
        session.commit()
        session.refresh(group)
    fake.quota_rules[rule.external_rule_id or rule.id] = {
        "ruleId": rule.external_rule_id or rule.id,
        "ruleName": rule.name,
        "quotaDimension": rule.quota_dimension,
        "quotaLimit": rule.quota_limit,
        "periodType": rule.period_type,
        "subjectType": "consumer_group",
    }
    fake.attach_consumer_group_to_rule(rule.gateway_id, rule.external_rule_id or rule.id, group.id)
    # fake subjects 里组主体带 usedAmount；fake 用量查询对组主体返回同值
    fake.group_usage[group.id] = group_used
    original_usage = fake.get_consumer_quota_usage

    def _usage(gateway_id, rule_id, consumer_id):
        fake.usage_calls.append(
            {"gateway_id": gateway_id, "rule_id": rule_id, "consumer_id": consumer_id}
        )
        if consumer_id == group.id:
            return {"requestId": "fake", "code": "200", "data": {"usedAmount": group_used}}
        return original_usage(gateway_id, rule_id, consumer_id)

    fake.get_consumer_quota_usage = _usage  # type: ignore[method-assign]


def test_usage_lists_group_scoped_subject_row():
    """组粒度规则的用量以组（csg-）为一行展示，配额只计一次，不按成员重复。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    _register_cloud_refs(fake, group, rule)
    _register_group_scoped_rule(session, fake, group, rule, group_used=900)

    # 本地两个消费者均归属该组（组内成员共享组粒度限额）
    for cid, name in (("cs-a", "成员A"), ("cs-b", "成员B")):
        session.add(
            ApiKeyConsumer(
                tenant_id=TENANT,
                name=name,
                gateway_id=rule.gateway_id,
                gateway_name=rule.gateway_name,
                external_consumer_id=cid,
                external_consumer_group_id=group.id,
                consumer_group_name=group.name,
                status="enabled",
                enable=True,
            )
        )
    session.commit()

    with patch.object(apk, "get_apig_client", return_value=fake):
        usage = list_usage(tenant_id=TENANT, db=session, current_user=admin)

    ids = [item.consumer_id for item in usage.items]
    # 组主体一行（csg- 前缀），成员消费者不重复出现组用量
    assert group.id in ids
    group_item = next(item for item in usage.items if item.consumer_id == group.id)
    assert group_item.used_amount == 900
    assert group_item.quota_limit == 3000
    assert group_item.consumer_name == "demo-consumer"
    # 成员消费者单独成行（无独立用量），不把组配额摊到每个成员重复计数
    member_items = [item for item in usage.items if item.consumer_id in ("cs-a", "cs-b")]
    for m in member_items:
        assert m.used_amount == 0
        assert m.quota_limit == 0
    # 汇总：组配额只计一次
    assert usage.summary.total_quota == 3000
    assert usage.summary.total_used == 900


def test_mine_usage_uses_group_subject_for_group_scoped_rule():
    """/mine/usage：组粒度规则的用量查询主体换成消费者归属的组（csg-）。"""
    session = _make_session()
    admin = _admin(session)
    member = _member(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    _register_cloud_refs(fake, group, rule)
    _register_group_scoped_rule(session, fake, group, rule, group_used=888)
    # 本地规则行落 subject_type=consumer_group（模拟同步回填后的状态）
    rule.subject_type = "consumer_group"
    session.add(rule)
    session.commit()

    created = create_application(
        ApiKeyApplicationCreate(tenant_id=TENANT, purpose="组粒度"),
        db=session,
        current_user=member,
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        approved = approve_application(
            created.id,
            ApiKeyApplicationApprove(
                tenant_id=TENANT,
                consumer_name="grp-member",
                consumer_group_id=group.id,
                # 生产中审批传云端 ruleId（= 本地行 external_rule_id），两者一致
                quota_rule_id=rule.external_rule_id or rule.id,
            ),
            db=session,
            current_user=admin,
        )

    with patch.object(apk, "get_apig_client", return_value=fake):
        items = list_my_usage(tenant_id=TENANT, db=session, current_user=member)

    assert len(items) == 1
    # 用量来自组主体（fake 对组主体返回 888），配额=组规则限额
    assert items[0].used_amount == 888
    assert items[0].quota_limit == 3000
    # 云端用量查询的 subject 是消费者归属的组 ID，而非消费者本人
    usage_calls = [c for c in fake.usage_calls if c["rule_id"] == (rule.external_rule_id or rule.id)]
    assert usage_calls and usage_calls[-1]["consumer_id"] == group.id


def test_update_consumer_group_owner_local_only():
    """修改消费组归属：纯本地业务字段更新，不调用阿里云；cloud 字段不被清。"""
    from app.api.api_key_applications import ApiKeyConsumerGroupOwnerUpdate, update_consumer_group_owner

    session = _make_session()
    admin = _admin(session)
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    # 与生产一致：组行持有云端组 ID
    group.external_consumer_group_id = "csg-own-test"
    session.add(group)
    session.commit()
    session.refresh(group)

    fake = FakeApigClient()

    # 1) 设置归属
    updated = update_consumer_group_owner(
        "csg-own-test",
        ApiKeyConsumerGroupOwnerUpdate(tenant_id=TENANT, owner="度假事业部"),
        db=session,
        current_user=admin,
    )
    assert updated.owner == "度假事业部"

    # 2) 未发起任何阿里云调用（用量/绑定/创建记录均为空）
    assert fake.created_consumers == []
    assert fake.created_rules == []
    assert fake.attached == []
    assert fake.attached_groups == []

    # 3) 清空归属（传空串）
    cleared = update_consumer_group_owner(
        "csg-own-test",
        ApiKeyConsumerGroupOwnerUpdate(tenant_id=TENANT, owner="  "),
        db=session,
        current_user=admin,
    )
    assert cleared.owner is None

    # 4) 不存在的组 → 404
    with pytest.raises(HTTPException) as exc_info:
        update_consumer_group_owner(
            "csg-not-exist",
            ApiKeyConsumerGroupOwnerUpdate(tenant_id=TENANT, owner="x"),
            db=session,
            current_user=admin,
        )
    assert exc_info.value.status_code == 404


def test_group_sync_does_not_overwrite_owner():
    """阿里云同步只覆盖云端字段，本地「归属」业务字段保留。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    group.external_consumer_group_id = "csg-sync-owner"
    group.owner = "度假事业部"
    session.add(group)
    session.commit()

    # 云端同名组（云端没有 owner 概念）
    fake.cloud_groups["csg-sync-owner"] = {
        "consumerGroupId": "csg-sync-owner",
        "name": "AILab",
        "description": "cloud desc",
        "consumerCount": 3,
        "gatewayType": "AI",
    }

    with patch.object(apk, "get_apig_client", return_value=fake):
        result = apk.sync_consumer_groups_from_aliyun(
            apk.ApiKeyAliyunSyncRequest(tenant_id=TENANT), db=session, current_user=admin
        )
    assert result.groups["updated"] == 1

    session.expire_all()
    row = session.exec(
        select(ApiKeyConsumerGroup).where(ApiKeyConsumerGroup.tenant_id == TENANT)
    ).one()
    # 云端字段已更新，业务字段保留
    assert row.name == "AILab"
    assert row.description == "cloud desc"
    assert row.consumer_count == 3
    assert row.owner == "度假事业部"


def test_create_quota_rule_with_consumer_subjects():
    """创建配额规则 consumer 粒度时可选传 consumer_ids 直接绑定消费者。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, _rule = _make_group_rule(session)
    _register_cloud_refs(fake, group, _rule)
    fake.consumers["cs-x"] = {"consumerId": "cs-x", "name": "x"}

    payload = ApiKeyQuotaRuleCreate(
        tenant_id=TENANT,
        name="consumer-quota",
        gateway_name="主力网关",
        quota_dimension="credit",
        quota_limit=5000,
        period_type="month",
        subject_type="consumer",
        consumer_ids=["cs-x"],
    )
    with patch.object(apk, "get_apig_client", return_value=fake):
        created = create_quota_rule(payload, db=session, current_user=admin)
    assert created.external_rule_id
    # fake 收到消费者粒度创建调用，带 consumer_ids
    last = fake.created_rules[-1]
    assert last.get("consumer_ids") == ["cs-x"]
    assert last.get("consumer_group_ids") is None


def test_update_quota_rule_group_add_remove():
    """编辑配额规则支持组增删（add_group_ids / remove_group_ids）。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    _register_cloud_refs(fake, group, rule)
    _register_group_scoped_rule(session, fake, group, rule, group_used=0)
    # 追加第二个组用于新增
    group2 = ApiKeyConsumerGroup(
        tenant_id=TENANT,
        name="组二",
        gateway_id=rule.gateway_id,
        gateway_name=rule.gateway_name,
        external_consumer_group_id="csg-two",
        status="enabled",
    )
    session.add(group2)
    session.commit()
    session.refresh(group2)
    fake.known_groups[group2.id] = {"consumerGroupId": group2.id, "name": "组二"}

    with patch.object(apk, "get_apig_client", return_value=fake):
        updated = apk.update_quota_rule_endpoint(
            rule.external_rule_id or rule.id,
            apk.ApiKeyQuotaRuleUpdate(
                tenant_id=TENANT,
                quota_limit=4000,
                add_group_ids=[group2.id],
                remove_group_ids=[group.id],
            ),
            db=session,
            current_user=admin,
        )
    assert updated.quota_limit == 4000
    # 云端被调用：addIds/removeIds 各一次
    assert any(a.get("consumer_group_id") == group2.id for a in fake.attached_groups)
    removed = [a.get("consumer_group_id") for a in fake.detached_groups] if hasattr(fake, "detached_groups") else []
    # detach_consumer_group_from_rule 从 attached_groups 移除并记录在 detached（fake 实现为过滤）
    assert group.id not in [a.get("consumer_group_id") for a in fake.attached_groups]


def test_list_quota_rule_subjects_endpoint():
    """subjects 查询端点：组主体归 groups，消费者主体只计数量。"""
    session = _make_session()
    admin = _admin(session)
    fake = FakeApigClient()
    group, rule = _make_group_rule(session, quota_limit=3000, period_type="month")
    _register_cloud_refs(fake, group, rule)
    _register_group_scoped_rule(session, fake, group, rule, group_used=0)
    # 追加一个消费者主体
    fake.attached.append({"consumer_id": "cs-live", "rule_id": rule.external_rule_id or rule.id})

    with patch.object(apk, "get_apig_client", return_value=fake):
        data = apk.list_quota_rule_subjects_endpoint(
            rule.external_rule_id or rule.id,
            tenant_id=TENANT,
            current_user=admin,
        )
    assert data["groups"] == [group.id]
    assert data["consumer_count"] == 1
