"""只读探针：查 api_key_consumer_groups / quota rules 按 tenant 分布，模拟审批弹窗数据面。"""
from sqlmodel import Session, select

from app.db import engine
from app.db.models import ApiKeyConsumerGroup, ApiKeyQuotaRule


def main() -> None:
    with Session(engine) as s:
        groups = s.exec(select(ApiKeyConsumerGroup)).all()
        rules = s.exec(select(ApiKeyQuotaRule)).all()
    print(f"groups total={len(groups)}")
    for g in groups:
        print(
            f"  tenant={g.tenant_id} name={g.name!r} external={g.external_consumer_group_id} "
            f"gateway={g.gateway_id}"
        )
    print(f"rules total={len(rules)}")
    for r in rules:
        print(
            f"  tenant={r.tenant_id} name={r.name!r} external={r.external_rule_id} "
            f"gateway={r.gateway_id} limit={r.quota_limit}/{r.period_type}"
        )
    # 模拟前端弹窗：按 tenant_demo 视角
    demo_groups = [g for g in groups if g.tenant_id == "tenant_demo"]
    demo_rules = [r for r in rules if r.tenant_id == "tenant_demo"]
    print(f"[tenant_demo] groups={len(demo_groups)} rules={len(demo_rules)}")
    if demo_groups:
        gw_ids = {g.gateway_id for g in demo_groups}
        compatible = [r for r in demo_rules if r.gateway_id in gw_ids]
        print(f"[tenant_demo] 同网关配额规则={len(compatible)}")


if __name__ == "__main__":
    main()
