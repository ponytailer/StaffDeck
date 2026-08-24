"""为「API Key 审批 / 配额管理 / 消费组管理」模块预置演示数据。

用法（在 backend 目录下）：
    python scripts/seed_api_key_mock.py

设计说明
======================================================================
- 覆盖前端 5 个 Tab 的展示：待审核 / 已分配密钥 / 审核历史 / 配额管理 / 消费组管理
- 已批准记录写入 used_amount + usage_month（当前月）缓存，
  使 /api/enterprise/api-key-applications/usage 直接命中缓存，
  展示稳定的水位预警数据（高水位 / 关注 / 正常），无需依赖 APIG mock 查询。
- 消费组与配额规则使用 mock 网关（gw-mock-001 主力 / gw-mock-002 备用），
  与 aliyun_aigw/config.py 的 mock 网关列表一致，审批弹窗可按网关过滤。
- 幂等：运行前清空 tenant_demo 下 API Key 相关三张表再重建，可重复执行。
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import select  # noqa: E402

from app.db.database import engine, init_db  # noqa: E402
from app.db.models import (  # noqa: E402
    ApiKeyApplication,
    ApiKeyConsumerGroup,
    ApiKeyQuotaRule,
    User,
    utc_now,
)
from app.security.auth import hash_password  # noqa: E402
from app.security.encryption import encrypt_secret  # noqa: E402

TENANT_ID = "tenant_demo"
ADMIN_USER_ID = "admin"

# mock 网关（与 aliyun_aigw/config.py USE_MOCK 分支保持一致）
GW_MAIN = {"id": "gw-mock-001", "name": "主力网关(演示)", "url": "https://apig-gw-mock-001.aliyuncs.com/v1"}
GW_BACKUP = {"id": "gw-mock-002", "name": "备用网关(演示)", "url": "https://apig-gw-mock-002.aliyuncs.com/v1"}


def _dt(days_ago: int, hour: int = 10, minute: int = 0) -> datetime:
    """生成过去第 days_ago 天的某个时刻（naive UTC，与 utc_now 一致）。"""
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0, microsecond=0)


def _reset(db_session) -> None:
    """清空演示租户下 API Key 相关表，保证脚本可重复执行。"""
    for model in (ApiKeyApplication, ApiKeyConsumerGroup, ApiKeyQuotaRule):
        rows = db_session.exec(select(model).where(model.tenant_id == TENANT_ID)).all()
        for r in rows:
            db_session.delete(r)
    db_session.commit()
    print("[seed] 已清空旧演示数据（api_key_applications / consumer_groups / quota_rules）")


def _ensure_users(db_session) -> dict[str, str]:
    """确保演示申请人存在，返回 {拼音username: 中文显示名}。"""
    members = [
        ("zhangwei", "张伟"),
        ("lina", "李娜"),
        ("wangqiang", "王强"),
        ("zhaomin", "赵敏"),
        ("chenjing", "陈静"),
        ("liuyang", "刘洋"),
    ]
    display = {"user_demo": "Demo User", "root": "root"}
    existing = {
        u.username: u
        for u in db_session.exec(
            select(User).where(User.tenant_id == TENANT_ID, User.username.in_([m[0] for m in members]))
        ).all()
    }
    for username, name in members:
        if username not in existing:
            db_session.add(
                User(
                    tenant_id=TENANT_ID,
                    username=username,
                    display_name=name,
                    role="member",
                    source="web",
                    password_hash=hash_password("demo1234"),
                )
            )
            print(f"[seed] 新增演示用户 {username}（{name}，密码 demo1234）")
    db_session.commit()
    return {**display, **dict(members)}


def _seed_groups(db_session) -> dict[str, str]:
    """创建消费组，返回 {name: id}。"""
    groups = [
        ApiKeyConsumerGroup(
            tenant_id=TENANT_ID,
            name="研发一组",
            description="核心研发团队，主力网关",
            owner="Club Med",
            gateway_id=GW_MAIN["id"],
            gateway_name=GW_MAIN["name"],
            external_consumer_id="cs-mock-101",
            created_by_user_id=ADMIN_USER_ID,
        ),
        ApiKeyConsumerGroup(
            tenant_id=TENANT_ID,
            name="研发二组",
            description="边缘服务与实验项目",
            owner="重庆项目",
            gateway_id=GW_BACKUP["id"],
            gateway_name=GW_BACKUP["name"],
            external_consumer_id="cs-mock-102",
            created_by_user_id=ADMIN_USER_ID,
        ),
        ApiKeyConsumerGroup(
            tenant_id=TENANT_ID,
            name="市场运营组",
            description="市场与运营部门，月度 Credit 预算",
            owner="复星总部IT",
            gateway_id=GW_MAIN["id"],
            gateway_name=GW_MAIN["name"],
            external_consumer_id="cs-mock-103",
            created_by_user_id=ADMIN_USER_ID,
        ),
    ]
    for g in groups:
        db_session.add(g)
    db_session.commit()
    result = {}
    for g in groups:
        db_session.refresh(g)
        result[g.name] = g.id
        print(f"[seed] 消费组 {g.name} -> {g.external_consumer_id}")
    return result


def _seed_rules(db_session) -> dict[str, str]:
    """创建配额规则，返回 {name: id}。"""
    rules = [
        ApiKeyQuotaRule(
            tenant_id=TENANT_ID,
            name="研发-日配额",
            gateway_id=GW_MAIN["id"],
            gateway_name=GW_MAIN["name"],
            quota_dimension="token",
            quota_limit=1_000_000,
            period_type="day",
            external_rule_id="qr-mock-101",
            created_by_user_id=ADMIN_USER_ID,
        ),
        ApiKeyQuotaRule(
            tenant_id=TENANT_ID,
            name="研发-周配额",
            gateway_id=GW_MAIN["id"],
            gateway_name=GW_MAIN["name"],
            quota_dimension="token",
            quota_limit=5_000_000,
            period_type="week",
            external_rule_id="qr-mock-102",
            created_by_user_id=ADMIN_USER_ID,
        ),
        ApiKeyQuotaRule(
            tenant_id=TENANT_ID,
            name="备用-月度配额",
            gateway_id=GW_BACKUP["id"],
            gateway_name=GW_BACKUP["name"],
            quota_dimension="token",
            quota_limit=20_000_000,
            period_type="month",
            external_rule_id="qr-mock-103",
            created_by_user_id=ADMIN_USER_ID,
        ),
        ApiKeyQuotaRule(
            tenant_id=TENANT_ID,
            name="市场-月度 Credit",
            gateway_id=GW_MAIN["id"],
            gateway_name=GW_MAIN["name"],
            quota_dimension="credit",
            quota_limit=100_000,
            period_type="month",
            external_rule_id="qr-mock-104",
            created_by_user_id=ADMIN_USER_ID,
        ),
    ]
    for r in rules:
        db_session.add(r)
    db_session.commit()
    result = {}
    for r in rules:
        db_session.refresh(r)
        result[r.name] = r.id
        print(f"[seed] 配额规则 {r.name} -> {r.external_rule_id}")
    return result


def _approved(
    *,
    display_name: str,
    purpose: str,
    group_id: str,
    group_name: str,
    rule_id: str,
    rule_name: str,
    rule_limit: int,
    rule_period: str,
    gw: dict,
    consumer_id: str,
    used: int,
    days_ago: int,
) -> ApiKeyApplication:
    """构造一条已批准记录（含用量缓存，usage_month=当前月）。"""
    api_key = "sk-" + "".join(
        f"{i:02x}" for i in bytes(range(1, 25))
    )  # 固定可读 mock key（加密存储，UI 仅显示掩码）
    return ApiKeyApplication(
        tenant_id=TENANT_ID,
        user_id=f"user_{display_name}",
        username=display_name,
        purpose=purpose,
        status="approved",
        api_key_encrypted=encrypt_secret(api_key),
        api_url=gw["url"],
        gateway_name=gw["name"],
        gateway_id=gw["id"],
        quota_limit=rule_limit,
        quota_period=rule_period,
        quota_rule_id=rule_id,
        quota_rule_name=rule_name,
        consumer_id=consumer_id,
        consumer_name=group_name,
        consumer_group_id=group_id,
        consumer_group_name=group_name,
        reviewer_user_id=ADMIN_USER_ID,
        reviewer_note="消费组与配额规则已配置，凭证已下发",
        reviewed_at=_dt(days_ago - 1),
        created_at=_dt(days_ago),
        updated_at=_dt(days_ago - 1),
        used_amount=used,
        usage_month=utc_now().strftime("%Y-%m"),
    )


def main() -> None:
    init_db()
    from sqlmodel import Session

    with Session(engine) as db_session:
        _reset(db_session)
        names = _ensure_users(db_session)
        groups = _seed_groups(db_session)
        rules = _seed_rules(db_session)

        rows: list[ApiKeyApplication] = []

        # ---- 待审核（3 条）----
        rows.append(
            ApiKeyApplication(
                tenant_id=TENANT_ID,
                user_id="user_zhangwei",
                username=names["zhangwei"],
                purpose="构建内部 AI 助手，需要调用通义千问模型接口",
                status="pending",
                created_at=_dt(0, hour=9, minute=24),
            )
        )
        rows.append(
            ApiKeyApplication(
                tenant_id=TENANT_ID,
                user_id="user_lina",
                username=names["lina"],
                purpose="文档知识库向量化与语义检索",
                status="pending",
                created_at=_dt(1, hour=15, minute=40),
            )
        )
        rows.append(
            ApiKeyApplication(
                tenant_id=TENANT_ID,
                user_id="user_wangqiang",
                username=names["wangqiang"],
                purpose="数据分析 Agent 需要访问大模型 API 进行报表生成",
                status="pending",
                created_at=_dt(0, hour=11, minute=2),
            )
        )

        # ---- 已批准（5 条，覆盖不同水位：2 高水位 / 1 关注 / 2 正常）----
        rows.append(
            _approved(
                display_name=names["zhaomin"],
                purpose="智能客服机器人联调与灰度上线",
                group_id=groups["研发一组"],
                group_name="研发一组",
                rule_id=rules["研发-日配额"],
                rule_name="研发-日配额",
                rule_limit=1_000_000,
                rule_period="day",
                gw=GW_MAIN,
                consumer_id="cs-mock-101",
                used=923_456,  # 92.3% -> 建议扩容
                days_ago=18,
            )
        )
        rows.append(
            _approved(
                display_name=names["chenjing"],
                purpose="代码评审助手（研发二组实验）",
                group_id=groups["研发一组"],
                group_name="研发一组",
                rule_id=rules["研发-日配额"],
                rule_name="研发-日配额",
                rule_limit=1_000_000,
                rule_period="day",
                gw=GW_MAIN,
                consumer_id="cs-mock-101",
                used=752_000,  # 75.2% -> 关注
                days_ago=15,
            )
        )
        rows.append(
            _approved(
                display_name=names["liuyang"],
                purpose="海外业务日报与报表生成",
                group_id=groups["研发二组"],
                group_name="研发二组",
                rule_id=rules["备用-月度配额"],
                rule_name="备用-月度配额",
                rule_limit=20_000_000,
                rule_period="month",
                gw=GW_BACKUP,
                consumer_id="cs-mock-102",
                used=5_120_000,  # 25.6% -> 正常
                days_ago=12,
            )
        )
        rows.append(
            _approved(
                display_name=names["user_demo"],
                purpose="演示账号联调测试",
                group_id=groups["市场运营组"],
                group_name="市场运营组",
                rule_id=rules["市场-月度 Credit"],
                rule_name="市场-月度 Credit",
                rule_limit=100_000,
                rule_period="month",
                gw=GW_MAIN,
                consumer_id="cs-mock-103",
                used=92_400,  # 92.4% -> 建议扩容
                days_ago=9,
            )
        )
        rows.append(
            _approved(
                display_name=names["root"],
                purpose="内部工具集成（监控大盘）",
                group_id=groups["市场运营组"],
                group_name="市场运营组",
                rule_id=rules["市场-月度 Credit"],
                rule_name="市场-月度 Credit",
                rule_limit=100_000,
                rule_period="month",
                gw=GW_MAIN,
                consumer_id="cs-mock-103",
                used=33_500,  # 33.5% -> 正常
                days_ago=6,
            )
        )

        # ---- 审核历史（1 驳回 + 1 吊销）----
        rows.append(
            ApiKeyApplication(
                tenant_id=TENANT_ID,
                user_id="user_wangqiang",
                username=names["wangqiang"],
                purpose="批量采集外部公开数据",
                status="rejected",
                reviewer_user_id=ADMIN_USER_ID,
                reviewer_note="用途涉及外部数据采集，缺少合规审批，请补充业务说明后重新申请",
                reviewed_at=_dt(8),
                created_at=_dt(9),
                updated_at=_dt(8),
            )
        )
        rows.append(
            ApiKeyApplication(
                tenant_id=TENANT_ID,
                user_id="user_chenjing",
                username=names["chenjing"],
                purpose="旧项目（已下线）密钥",
                status="revoked",
                reviewer_user_id=ADMIN_USER_ID,
                reviewer_note="项目已下线，密钥回收",
                reviewed_at=_dt(5),
                created_at=_dt(20),
                updated_at=_dt(5),
            )
        )

        for r in rows:
            db_session.add(r)
        db_session.commit()

        # ---- 汇总 ----
        pending = sum(1 for r in rows if r.status == "pending")
        approved = sum(1 for r in rows if r.status == "approved")
        history = sum(1 for r in rows if r.status in ("rejected", "revoked"))
        print("\n[seed] 完成，演示数据摘要：")
        print(f"  待审核 {pending} 条 / 已分配 {approved} 条 / 审核历史 {history} 条")
        print(f"  消费组 {len(groups)} 个 / 配额规则 {len(rules)} 条")
        print("  已批准记录已写入本月用量缓存，配额管理 Tab 可直接看到水位预警")


if __name__ == "__main__":
    main()
