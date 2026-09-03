"""诊断阿里云 AI Gateway 真实数据：消费者 / 消费者组 / 配额规则 / 配额主体。

用法：
    cd backend && .venv/bin/python scripts/diag_aliyun_apig.py
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from app.aliyun_aigw import consumers as c_consumers
from app.aliyun_aigw import quota as c_quota
from app.aliyun_aigw.config import get_gateway_configs
from app.aliyun_aigw.client import _request


def pprint(label: str, obj) -> None:
    print(f"\n===== {label} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    configs = get_gateway_configs()
    print("网关列表：", [(c.name, c.gateway_id) for c in configs])
    gw = configs[0] if configs else None
    if not gw:
        print("!! 没有网关配置")
        return

    # 1) ListConsumers 原始返回
    try:
        raw = _request(
            "GET",
            "/v1/consumers",
            action="ListConsumers",
            query={"gatewayType": "AI", "pageNumber": 1, "pageSize": 100},
        )
        pprint("ListConsumers 原始返回", raw)
    except Exception as exc:  # noqa: BLE001
        print("ListConsumers 失败:", exc)

    # 2) 遍历每个 consumer 详情（看完整字段）
    try:
        items = c_consumers.list_consumers(gateway_type="AI")
        print(f"\n===== 消费者共 {len(items)} 个 =====")
        for item in items:
            pprint("consumer item", item)
            cid = item.get("consumerId")
            if cid:
                try:
                    detail = c_consumers.get_consumer(cid)
                    pprint(f"GetConsumer {cid} 详情", detail)
                except Exception as exc:  # noqa: BLE001
                    print(f"GetConsumer {cid} 失败:", exc)
    except Exception as exc:  # noqa: BLE001
        print("list_consumers 失败:", exc)

    # 3) 配额规则列表
    try:
        rules = c_quota.list_quota_rules(gateway_id=gw.gateway_id)
        print(f"\n===== 配额规则共 {len(rules)} 条 =====")
        for r in rules:
            pprint("rule item", r)
            rid = r.get("ruleId")
            if rid:
                try:
                    detail = c_quota.get_quota_rule(rid, gateway_id=gw.gateway_id)
                    pprint(f"GetQuotaRule {rid} 详情", detail)
                except Exception as exc:  # noqa: BLE001
                    print(f"GetQuotaRule {rid} 失败:", exc)
                try:
                    subjects = c_quota.list_quota_rule_subjects(rid, gateway_id=gw.gateway_id)
                    pprint(f"ListQuotaRuleSubjects {rid} 主体", subjects)
                except Exception as exc:  # noqa: BLE001
                    print(f"ListQuotaRuleSubjects {rid} 失败:", exc)
    except Exception as exc:  # noqa: BLE001
        print("list_quota_rules 失败:", exc)

    # 4) 尝试 ListConsumerGroups（判断是否存在该接口）
    for path, action in (
        ("/v1/consumer-groups", "ListConsumerGroups"),
        ("/v1/gateways/{}/consumer-groups".format(gw.gateway_id), "ListGatewayConsumerGroups"),
    ):
        try:
            resp = _request("GET", path, action=action, query={"pageNumber": 1, "pageSize": 100})
            pprint(f"{action} 原始返回", resp)
        except Exception as exc:  # noqa: BLE001
            print(f"{action} 失败: {exc}")


if __name__ == "__main__":
    main()