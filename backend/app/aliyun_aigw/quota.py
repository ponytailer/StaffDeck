"""配额规则（QuotaRule）CRUD 与配额用量查询业务方法。

已对接接口：
- GetGatewayQuotaRuleSubjectUsage
      GET /v1/gateways/{gatewayId}/quota-rules/{ruleId}/subjects/{subjectId}/usage
- AddGatewayQuotaRule               POST /v1/gateways/{gatewayId}/quota-rules
- ListGatewayQuotaRules             GET  /v1/gateways/{gatewayId}/quota-rules
- GetGatewayQuotaRule               GET  /v1/gateways/{gatewayId}/quota-rules/{ruleId}
- UpdateGatewayQuotaRule            PUT  /v1/gateways/{gatewayId}/quota-rules/{ruleId}
- DeleteGatewayQuotaRule            DELETE /v1/gateways/{gatewayId}/quota-rules/{ruleId}
"""
from __future__ import annotations

from typing import Any

from .client import _request
from .config import DEFAULT_SUBJECT_ID, GATEWAY_ID, QUOTA_RULE_ID, USE_MOCK


def _resolve_gateway_id(gateway_id: str | None) -> str:
    """解析真实网关 ID；mock 模式缺省时给占位值，避免路径非法。"""
    return gateway_id or GATEWAY_ID or ("gw-mock" if USE_MOCK else "")


def get_quota_usage(
    subject_id: str | None = None,
    *,
    gateway_id: str | None = None,
    rule_id: str | None = None,
    page_number: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    """获取某消费者在配额规则下的用量。

    subject_id 缺省时回退到环境变量 ALIYUN_APIG_SUBJECT_ID。
    gateway_id / rule_id 优先用参数，回退到环境变量 ALIYUN_APIG_GATEWAY_ID / ALIYUN_APIG_QUOTA_RULE_ID。
    """
    subject_id = subject_id or DEFAULT_SUBJECT_ID
    if USE_MOCK:
        # Mock 模式无需真实网关/主体配置
        from .mock import _mock_quota_usage

        return _mock_quota_usage(subject_id=subject_id, rule_id=rule_id)

    if not subject_id:
        raise ValueError("缺少 subject_id（传参或配置 ALIYUN_APIG_SUBJECT_ID）")
    gid = _resolve_gateway_id(gateway_id)
    rid = rule_id or QUOTA_RULE_ID
    if not (gid and rid):
        raise ValueError(
            "缺少网关/配额规则配置：ALIYUN_APIG_GATEWAY_ID / ALIYUN_APIG_QUOTA_RULE_ID"
        )

    path = f"/v1/gateways/{gid}/quota-rules/{rid}/subjects/{subject_id}/usage"
    query = {"pageNumber": page_number, "pageSize": min(page_size, 10)}
    return _request("GET", path, action="GetGatewayQuotaRuleSubjectUsage", query=query)


def create_quota_rule(
    rule_name: str,
    quota_dimension: str,
    quota_limit: int,
    *,
    gateway_id: str | None = None,
    period_type: str = "day",
    window_alignment: str = "calendar",
    timezone: str = "UTC+8",
    consumer_ids: list[str] | None = None,
    consumer_group_ids: list[str] | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """新增配额规则（AddGatewayQuotaRule）。

    两种主体粒度（AI 网关 2.1.21+ 支持，2.1.23 已实测可用）：
    - consumer_ids：消费者粒度（subjectType=consumer，默认）
    - consumer_group_ids：消费组粒度（subjectType=consumer_group）
    两者互斥，同时传入以消费组为准（云端要求互斥，此处显式防护）。
    """
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    if consumer_ids and consumer_group_ids:
        raise ValueError("consumer_ids 与 consumer_group_ids 互斥，不能同时传入")
    subject_type = "consumer_group" if consumer_group_ids else "consumer"
    body: dict[str, Any] = {
        "ruleName": rule_name,
        "quotaDimension": quota_dimension,
        "quotaLimit": int(quota_limit),
        "periodType": period_type,
        "windowAlignment": window_alignment,
        "timezone": timezone,
        "subjectType": subject_type,
    }
    if consumer_group_ids:
        body["consumerGroupIds"] = consumer_group_ids
    elif consumer_ids:
        body["consumerIds"] = consumer_ids
    if dry_run:
        body["dryRun"] = True
    if overwrite:
        body["overwrite"] = True
    return _request(
        "POST",
        f"/v1/gateways/{gid}/quota-rules",
        action="AddGatewayQuotaRule",
        body=body,
    )


def list_quota_rules(
    keyword: str | None = None,
    *,
    gateway_id: str | None = None,
    page_number: int = 1,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """列出配额规则（ListGatewayQuotaRules）。"""
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    query: dict[str, Any] = {"pageNumber": page_number, "pageSize": page_size}
    if keyword:
        query["keyword"] = keyword
    resp = _request(
        "GET",
        f"/v1/gateways/{gid}/quota-rules",
        action="ListGatewayQuotaRules",
        query=query,
    )
    data = resp.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    return items or []


def list_quota_rule_subjects(
    rule_id: str,
    *,
    gateway_id: str | None = None,
    page_number: int = 1,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    """列出配额规则下绑定的主体（ListGatewayQuotaRuleSubjects）。

    ListGatewayQuotaRules 不返回 consumerIds，需单独查 subjects 才能知道
    规则当前覆盖了哪些消费者。返回项形如：
        {"subjectType":"consumer","name":"ailab_xxx","id":"cs-xxx",
         "usedAmount":157,"quotaLimit":5000,"overLimit":false,"quotaDimension":"credit"}
    """
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    resp = _request(
        "GET",
        f"/v1/gateways/{gid}/quota-rules/{rule_id}/subjects",
        action="ListGatewayQuotaRuleSubjects",
        query={"pageNumber": page_number, "pageSize": page_size},
    )
    data = resp.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    return items or []


def get_quota_rule(rule_id: str, *, gateway_id: str | None = None) -> dict[str, Any]:
    """获取单条配额规则（GetGatewayQuotaRule）。"""
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    resp = _request(
        "GET",
        f"/v1/gateways/{gid}/quota-rules/{rule_id}",
        action="GetGatewayQuotaRule",
    )
    return resp.get("data") or resp


def update_quota_rule(
    rule_id: str,
    *,
    gateway_id: str | None = None,
    rule_name: str | None = None,
    quota_limit: int | None = None,
    add_ids: list[str] | None = None,
    remove_ids: list[str] | None = None,
    add_group_ids: list[str] | None = None,
    remove_group_ids: list[str] | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    """编辑配额规则（UpdateGatewayQuotaRule）。

    add_ids/remove_ids：消费者主体（cs-）；add_group_ids/remove_group_ids：
    消费组主体（csg-，网关 2.1.21+ 支持消费组粒度）。同一次调用内消费者
    与消费组可混合提交，云端按主体类型分别处理。
    """
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    body: dict[str, Any] = {}
    if rule_name is not None:
        body["ruleName"] = rule_name
    if quota_limit is not None:
        body["quotaLimit"] = int(quota_limit)
    merged_add = list(add_ids or []) + list(add_group_ids or [])
    merged_remove = list(remove_ids or []) + list(remove_group_ids or [])
    if merged_add:
        body["addIds"] = merged_add
    if merged_remove:
        body["removeIds"] = merged_remove
    if dry_run:
        body["dryRun"] = True
    if overwrite:
        body["overwrite"] = True
    return _request(
        "PUT",
        f"/v1/gateways/{gid}/quota-rules/{rule_id}",
        action="UpdateGatewayQuotaRule",
        body=body,
    )


def delete_quota_rule(rule_id: str, *, gateway_id: str | None = None) -> dict[str, Any]:
    """删除配额规则（DeleteGatewayQuotaRule）。"""
    gid = _resolve_gateway_id(gateway_id)
    if not USE_MOCK and not gid:
        raise ValueError("缺少网关配置：ALIYUN_APIG_GATEWAY_ID 或传入 gateway_id")
    return _request(
        "DELETE",
        f"/v1/gateways/{gid}/quota-rules/{rule_id}",
        action="DeleteGatewayQuotaRule",
    )
