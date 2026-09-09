"""一次性修复：day/week 粒度的当月快照行被旧 max 护栏卡在高值的问题。

背景：快照 upsert 曾对所有规则统一取 max(old, new)，该「月内单调递增」
假设只对 month 粒度成立。day/week 粒度的云端周期会重置（日/周清零），
重置后的真实低值被 max 挡在快照外，配额页持续显示旧用量。

修复代码上线后，仍被查询的消费者会在下一次用量查询时自动写入云端新值
（自愈）。本脚本处理两类自愈不到的存量行：
1. 已不再被实时查询覆盖的快照行（消费者换规则/被删除）；
2. 希望立即纠正面板显示、不等下一次查询的场景。

用法（服务器）：
    cd backend && .venv/bin/python scripts/fix_day_week_snapshots.py           # 预览（dry-run）
    cd backend && .venv/bin/python scripts/fix_day_week_snapshots.py --apply   # 实际执行

行为：
- 找出当月 quota_period ∈ {day, week} 的快照行，逐行回源阿里云
  GetConsumerQuotaUsage（主体按规则 subject_type 换算消费者/消费组）；
- 云端值低于快照值 → 更新为云端值（应用模式）；
- 云端查询失败 → 保留原值并在输出中标注。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.aliyun_aigw import AliyunApigError, get_apig_client  # noqa: E402
from app.config import get_settings  # noqa: E402

TZ_OFFSET = 8  # 服务器 DB 存 UTC，输出转北京时间显示


def _current_month() -> str:
    # 北京时间的自然月（快照 month 以用户视角的月为准）
    from datetime import timedelta

    return (datetime.utcnow() + timedelta(hours=TZ_OFFSET)).strftime("%Y-%m")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际执行修复（缺省 dry-run）")
    args = parser.parse_args()

    from app.db.database import engine

    month = _current_month()
    print(f"当月（北京时间）：{month}  模式：{'APPLY' if args.apply else 'DRY-RUN'}")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, tenant_id, consumer_id, consumer_name, gateway_id,
                       quota_rule_id, quota_rule_name, quota_limit, quota_period, used_amount
                FROM api_key_usage_snapshots
                WHERE month = :month AND quota_period IN ('day', 'week')
                """
            ),
            {"month": month},
        ).mappings().fetchall()

    if not rows:
        print("无 day/week 粒度的当月快照行，无需修复。")
        return
    print(f"待检快照行：{len(rows)} 条")

    client = get_apig_client()
    if client is None:
        print("[!] 阿里云 APIG 客户端不可用（未配置 AK/SK 或 mock 未开启），无法回源。")
        return

    # 规则 subject_type 映射：组粒度主体用组 ID 查询
    tenant_ids = {row["tenant_id"] for row in rows}
    rule_ids = {row["quota_rule_id"] for row in rows}
    subject_type_by_rule: dict[str, str] = {}
    consumer_group_by_consumer: dict[str, str] = {}
    with engine.connect() as conn:
        for tenant_id in tenant_ids:
            for row in conn.execute(
                text(
                    "SELECT external_rule_id, subject_type FROM api_key_quota_rules "
                    "WHERE tenant_id = :t AND external_rule_id IS NOT NULL"
                ),
                {"t": tenant_id},
            ).mappings():
                subject_type_by_rule[row["external_rule_id"]] = row["subject_type"] or "consumer"
            for row in conn.execute(
                text(
                    "SELECT external_consumer_id, external_consumer_group_id "
                    "FROM api_key_consumers WHERE tenant_id = :t AND external_consumer_id IS NOT NULL"
                ),
                {"t": tenant_id},
            ).mappings():
                consumer_group_by_consumer[row["external_consumer_id"]] = (
                    row["external_consumer_group_id"] or ""
                )

    fixed = kept_failed = kept_equal_or_higher = 0
    for row in rows:
        rule_id = row["quota_rule_id"]
        subject_id = row["consumer_id"]
        if (subject_type_by_rule.get(rule_id) or "consumer") == "consumer_group":
            group_id = consumer_group_by_consumer.get(subject_id, "")
            if group_id:
                subject_id = group_id
        used_in_cloud: int | None = None
        try:
            resp = client.get_consumer_quota_usage(
                gateway_id=row["gateway_id"] or "",
                rule_id=rule_id,
                consumer_id=subject_id,
            )
            data = resp.get("data") if isinstance(resp, dict) else {}
            used_in_cloud = int(data.get("usedAmount") or 0)
        except (AliyunApigError, RuntimeError, ValueError) as exc:
            print(f"  [!] {row['consumer_id']} ({row['quota_period']}) 查询失败，保留原值：{exc}")
            kept_failed += 1
            continue
        old_used = int(row["used_amount"] or 0)
        if used_in_cloud >= old_used:
            kept_equal_or_higher += 1
            continue
        print(
            f"  [fix] {row['consumer_id']} ({row['consumer_name'] or ''}) "
            f"rule={row['quota_rule_name'] or rule_id} period={row['quota_period']} "
            f"快照 {old_used} → 云端 {used_in_cloud}"
        )
        if args.apply:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE api_key_usage_snapshots SET used_amount = :u, updated_at = :t "
                        "WHERE id = :id"
                    ),
                    {"u": used_in_cloud, "t": datetime.utcnow(), "id": row["id"]},
                )
            fixed += 1

    print(
        f"\n汇总：需下调 {fixed + (len(rows) - kept_failed - kept_equal_or_higher) if not args.apply else fixed} 条，"
        f"已下调 {fixed if args.apply else 0} 条，"
        f"云端值不低于快照（无需改）{kept_equal_or_higher} 条，查询失败 {kept_failed} 条"
    )
    if not args.apply:
        print("（dry-run 未写库；确认无误后加 --apply 执行）")


if __name__ == "__main__":
    if get_settings() is None:  # pragma: no cover - 配置缺失时尽早报错
        sys.exit(1)
    main()
