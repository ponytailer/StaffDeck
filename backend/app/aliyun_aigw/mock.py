"""Mock 数据分支（仅演示用，生产删除/注释本段即可）。

与真实接口返回结构保持一致，用于驱动图表与列表展示。
"""
from __future__ import annotations

import re
import uuid
from typing import Any

# 内存态配额规则（仅 mock 模式使用，用于模拟 CRUD 的持久效果）
_MOCK_QUOTA_RULES: list[dict[str, Any]] = [
    {
        "ruleId": "qr-mock-001",
        "ruleName": "default-token",
        "quotaDimension": "token",
        "quotaLimit": 1000,
        "ruleStatus": "enabled",
        "periodType": "day",
        "timezone": "UTC+8",
        "windowAlignment": "calendar",
        "consumerIds": ["cs-mock-001"],
    },
    {
        "ruleId": "qr-mock-002",
        "ruleName": "team-credit-weekly",
        "quotaDimension": "credit",
        "quotaLimit": 5000,
        "ruleStatus": "enabled",
        "periodType": "week",
        "timezone": "UTC+8",
        "windowAlignment": "calendar",
        "consumerIds": [],
    },
]

# 内存态消费者（仅 mock 模式；消费组创建/审批追加 Key 均作用于此）
_MOCK_CONSUMERS: dict[str, dict[str, Any]] = {
    "cs-mock-001": {
        "consumerId": "cs-mock-001",
        "name": "demo-consumer",
        "description": "演示消费者",
        "gatewayType": "AI",
        "enable": True,
        "apikeys": [],
    },
    "cs-mock-002": {
        "consumerId": "cs-mock-002",
        "name": "team-alpha",
        "description": "Alpha 团队网关消费者",
        "gatewayType": "AI",
        "enable": True,
        "apikeys": [],
    },
}


def _mock_quota_usage(
    subject_id: str | None = None,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """与 GetGatewayQuotaRuleSubjectUsage 真实返回结构一致的 mock。

    根据 subject_id / rule_id 生成确定性用量，使不同消费者/规则展示不同水位。
    """
    seed = hash((subject_id or "") + (rule_id or ""))
    base_models = [
        ("qwen-plus", 120, 80, 10),
        ("qwen-max", 90, 60, 8),
        ("deepseek-v3", 70, 30, 10),
        ("glm-4", 30, 10, 5),
    ]
    items = []
    for idx, (model, i_base, o_base, c_base) in enumerate(base_models):
        factor = 0.5 + ((seed + idx * 131) % 100) / 100.0
        item = {
            "model": model,
            "inputAmount": int(i_base * factor),
            "outputAmount": int(o_base * factor),
            "cachedAmount": int(c_base * factor),
            "usedAmount": int((i_base + o_base + c_base) * factor),
            "startTime": f"2026-08-{5 + idx:02d} {(13 + idx):02d}:16:31",
        }
        items.append(item)
    totals = {
        k: sum(i.get(k, 0) for i in items)
        for k in ("inputAmount", "outputAmount", "cachedAmount", "usedAmount")
    }
    return {
        "requestId": "mock-" + uuid.uuid4().hex[:12],
        "code": "200",
        "message": "success",
        "data": {
            "usedAmount": totals["usedAmount"],
            "totalQuota": 1000,
            "overLimit": totals["usedAmount"] > 1000,
            "inputAmount": totals["inputAmount"],
            "outputAmount": totals["outputAmount"],
            "cachedAmount": totals["cachedAmount"],
            "details": {
                "totalSize": len(items),
                "pageNumber": 1,
                "pageSize": 10,
                "items": items,
            },
        },
    }


def _mock_dispatch(
    method: str,
    path: str,
    query: dict[str, Any] | None,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    if method == "POST" and path == "/v1/consumers":
        name = (body or {}).get("name", "mock-consumer")
        consumer_id = "cs-mock-" + uuid.uuid4().hex[:8]
        # Custom 模式：明文由调用方传入；System 模式：mock 自动生成
        apikey = ""
        for src in ((body or {}).get("apiKeyIdentityConfig") or {}).get("apiKeySources", []):
            for cred in src.get("credentials", []):
                if cred.get("apikey"):
                    apikey = cred["apikey"]
                elif cred.get("generateMode") == "System" and not apikey:
                    apikey = "sk-mock-" + uuid.uuid4().hex[:24]
        consumer = {
            "consumerId": consumer_id,
            "name": name,
            "description": (body or {}).get("description", ""),
            "gatewayType": (body or {}).get("gatewayType", "AI"),
            "enable": (body or {}).get("enable", True),
            "apikeys": [apikey] if apikey else [],
            "consumerGroups": [],
        }
        _MOCK_CONSUMERS[consumer_id] = consumer
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
            "data": {"consumerId": consumer_id},
            "_mock_name": name,
        }
    if method == "DELETE" and path.startswith("/v1/consumers/"):
        cid = path.rsplit("/", 1)[-1]
        _MOCK_CONSUMERS.pop(cid, None)
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
        }
    if method == "PUT" and path.startswith("/v1/consumers/"):
        cid = path.rsplit("/", 1)[-1]
        consumer = _MOCK_CONSUMERS.get(cid)
        b = body or {}
        if consumer:
            if "description" in b:
                consumer["description"] = b["description"]
            if "enable" in b:
                consumer["enable"] = b["enable"]
            # 绑定消费组（csg- 列表，全量覆盖）
            if "consumerGroupIds" in b:
                consumer["consumerGroups"] = [
                    {"consumerGroupId": csg, "name": csg}
                    for csg in (b["consumerGroupIds"] or [])
                ]
            # 自定义 API Key 凭证：_mock_credential_replace=True 时全量覆盖（裁剪语义），否则追加
            identity = b.get("apiKeyIdentityConfig") or b.get("apikeyIdentityConfig") or {}
            replace_mode = identity.pop("_mock_credential_replace", False)
            for src in identity.get("apiKeySources", []) + identity.get("apikeySources", []):
                creds = src.get("credentials", [])
                if replace_mode:
                    consumer["apikeys"] = [
                        cred.get("apikey") for cred in creds if cred.get("apikey")
                    ]
                else:
                    for cred in creds:
                        ak = cred.get("apikey")
                        if ak and ak not in consumer["apikeys"]:
                            consumer["apikeys"].append(ak)
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
            "data": {"consumerId": cid},
        }
    if method == "GET" and path == "/v1/consumers":
        items = []
        for c in _MOCK_CONSUMERS.values():
            items.append({
                "consumerId": c["consumerId"],
                "name": c["name"],
                "description": c["description"],
                "gatewayType": c["gatewayType"],
                "enable": c["enable"],
            })
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
            "data": {"items": items, "total": len(items)},
        }
    if method == "GET" and path.startswith("/v1/consumers/"):
        cid = path.rsplit("/", 1)[-1]
        c = _MOCK_CONSUMERS.get(cid)
        if c is None:
            return {
                "requestId": "mock-" + uuid.uuid4().hex[:12],
                "code": "NotFound",
                "message": f"consumer {cid} not found",
            }
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
            "data": {
                "consumerId": c["consumerId"],
                "name": c["name"],
                "description": c["description"],
                "gatewayType": c["gatewayType"],
                "enable": c["enable"],
                "consumerGroups": c.get("consumerGroups", []),
                "apiKeyIdentityConfig": {
                    "type": "Apikey",
                    "apiKeySources": [
                        {
                            "source": "Default",
                            "value": "Authorization",
                            "credentials": [
                                {"generateMode": "System", "apikey": ak}
                            ],
                        }
                        for ak in c.get("apikeys", [])
                    ],
                },
            },
        }
    # 批量加入消费者组（BatchAddConsumerGroupConsumers）
    _batch_add = re.match(r"/v1/consumer-groups/([^/]+)/consumers/batch-add$", path)
    if _batch_add and method == "POST":
        csg = _batch_add.group(1)
        ids = (body or {}).get("consumerIds") or []
        success, skipped, failed = [], [], []
        for cid in ids:
            consumer = _MOCK_CONSUMERS.get(cid)
            if consumer is None:
                failed.append(cid)
            elif any(g["consumerGroupId"] == csg for g in consumer.get("consumerGroups", [])):
                skipped.append(cid)
            else:
                consumer.setdefault("consumerGroups", []).append(
                    {"consumerGroupId": csg, "name": csg}
                )
                success.append(cid)
        return {
            "requestId": "mock-" + uuid.uuid4().hex[:12],
            "code": "Ok",
            "message": "success",
            "data": {
                "successConsumerIds": success,
                "skippedConsumerIds": skipped,
                "failedConsumerIds": failed,
            },
        }
    # 配额规则 CRUD（mock）
    _qlist = re.match(r"/v1/gateways/[^/]+/quota-rules$", path)
    _qsingle = re.match(r"/v1/gateways/[^/]+/quota-rules/([^/]+)$", path)
    if _qlist and method == "GET":
        return {
            "requestId": "mock", "code": "Ok", "message": "success",
            "data": {"items": list(_MOCK_QUOTA_RULES), "totalSize": len(_MOCK_QUOTA_RULES)},
        }
    if _qlist and method == "POST":
        b = body or {}
        # dryRun 预检：返回 accepted 预览，不落库
        if b.get("dryRun"):
            return {
                "requestId": "mock",
                "code": "Ok",
                "message": "success",
                "data": {"accepted": True},
            }
        rid = "qr-mock-" + uuid.uuid4().hex[:8]
        rule = {
            "ruleId": rid,
            "ruleName": (b.get("ruleName") or "rule"),
            "quotaDimension": b.get("quotaDimension", "token"),
            "quotaLimit": int(b.get("quotaLimit") or 0),
            "ruleStatus": "enabled",
            "periodType": b.get("periodType", "day"),
            "timezone": b.get("timezone", "UTC+8"),
            "windowAlignment": b.get("windowAlignment", "calendar"),
            "consumerIds": list(b.get("consumerIds") or []),
        }
        _MOCK_QUOTA_RULES.append(rule)
        return {"requestId": "mock", "code": "Ok", "message": "success", "data": {"ruleId": rid}}
    if _qsingle:
        rid = _qsingle.group(1)
        if method == "GET":
            rule = next((r for r in _MOCK_QUOTA_RULES if r["ruleId"] == rid), None)
            return {"requestId": "mock", "code": "Ok", "message": "success", "data": rule or {}}
        if method == "PUT":
            rule = next((r for r in _MOCK_QUOTA_RULES if r["ruleId"] == rid), None)
            if rule:
                b = body or {}
                if "ruleName" in b:
                    rule["ruleName"] = b["ruleName"]
                if "quotaLimit" in b:
                    rule["quotaLimit"] = int(b["quotaLimit"])
                if b.get("addIds"):
                    rule.setdefault("consumerIds", [])
                    for cid in b["addIds"]:
                        if cid not in rule["consumerIds"]:
                            rule["consumerIds"].append(cid)
                if b.get("removeIds"):
                    rule["consumerIds"] = [
                        c for c in rule.get("consumerIds", []) if c not in b["removeIds"]
                    ]
            return {"requestId": "mock", "code": "Ok", "message": "success", "data": rule or {}}
        if method == "DELETE":
            _MOCK_QUOTA_RULES[:] = [r for r in _MOCK_QUOTA_RULES if r["ruleId"] != rid]
            return {"requestId": "mock", "code": "Ok", "message": "success"}
    if method == "GET" and "/usage" in path:
        return _mock_quota_usage()
    return {"requestId": "mock", "code": "Ok", "message": "success", "data": {}}
