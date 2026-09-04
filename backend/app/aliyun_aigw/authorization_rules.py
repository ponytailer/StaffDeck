"""消费者授权规则（ConsumerAuthorizationRule）管理。

实测结论（2026-09）：
- 消费者凭证正确 ≠ 可以调用 AI API。AI 网关还要求「消费者→AI API」的授权关系，
  缺失时调用网关返回 401（即使 Key 正确、deployStatus=Success）。
- 授权规则接口（APIG 2024-03-27）：
  - CreateConsumerAuthorizationRules  POST /v1/authorization-rules
  - QueryConsumerAuthorizationRules   GET  /v1/authorization-rules
  - DeleteConsumerAuthorizationRule   DELETE /v1/consumer-authorization-rules/{id}
    （注意路径是 /v1/consumer-authorization-rules，与 list/query 的 /v1/authorization-rules 不同）
- resourceType 对 LLM 类型 API 必须传 "LLM"（"HttpApi"/"API" 均无效），且必须带 environmentId。
- 同一授权资源也可授予消费者组（principalType=ConsumerGroup），组内成员共享授权。
"""
from __future__ import annotations

from typing import Any

from .client import _request


def create_authorization_rule(
    consumer_id: str,
    *,
    api_id: str,
    environment_id: str,
    principal_type: str = "Consumer",
    consumer_group_id: str | None = None,
) -> dict[str, Any]:
    """创建一条消费者→AI API 的授权规则。

    principal_type=Consumer 时 consumer_id 必填；
    principal_type=ConsumerGroup 时 consumer_group_id 必填。
    返回 {consumerAuthorizationRuleIds: [car-...]}。
    """
    identifier: dict[str, Any] = {"resourceId": api_id, "environmentId": environment_id}
    rule: dict[str, Any] = {
        "principalType": principal_type,
        "resourceType": "LLM",
        "resourceIdentifier": identifier,
        "expireMode": "LongTerm",
    }
    if principal_type == "ConsumerGroup":
        rule["consumerGroupId"] = consumer_group_id or ""
    else:
        rule["consumerId"] = consumer_id
    body = {"authorizationRules": [rule]}
    return _request(
        "POST", "/v1/authorization-rules",
        action="CreateConsumerAuthorizationRules", body=body,
    )


def query_authorization_rules(
    *,
    consumer_id: str | None = None,
    consumer_group_id: str | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """查询授权规则列表（至少提供 consumer_id / consumer_group_id 之一）。"""
    query: dict[str, Any] = {"pageNumber": 1, "pageSize": page_size}
    if consumer_id:
        query["consumerId"] = consumer_id
    if consumer_group_id:
        query["consumerGroupId"] = consumer_group_id
    resp = _request(
        "GET", "/v1/authorization-rules",
        action="QueryConsumerAuthorizationRules", query=query,
    )
    data = resp.get("data") or {}
    return data.get("items") or []


def has_api_authorization(consumer_id: str, api_id: str) -> bool:
    """判断消费者对指定 AI API 是否已有授权规则。"""
    for item in query_authorization_rules(consumer_id=consumer_id):
        api_info = item.get("apiInfo") or {}
        if api_info.get("httpApiId") == api_id:
            return True
    return False


def list_llm_apis(gateway_id: str | None = None) -> list[dict[str, Any]]:
    """列出 AI 网关下的 LLM 类型 API（授权目标候选）。

    ListHttpApis 的列表项中，LLM API 的真实 ID 藏在 versionedHttpApis[0].httpApiId；
    环境信息在 deployConfigs[].environmentId。
    返回 [{httpApiId, name, environmentId, gatewayId}, ...]。

    gateway_id 过滤为空时回退全量（实测账号通常仅一个 AI 网关；
    mock 场景网关 ID 与配置不一致时也能兜底）。
    """
    query: dict[str, Any] = {"gatewayType": "AI", "pageNumber": 1, "pageSize": 50}
    resp = _request("GET", "/v1/http-apis", action="ListHttpApis", query=query)
    items = (resp.get("data") or {}).get("items") or []
    result: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    for it in items:
        if it.get("type") != "LLM":
            continue
        for ver in it.get("versionedHttpApis") or []:
            for dc in ver.get("deployConfigs") or []:
                entry = {
                    "httpApiId": ver.get("httpApiId"),
                    "name": ver.get("name") or it.get("name"),
                    "environmentId": dc.get("environmentId"),
                    "gatewayId": dc.get("gatewayId"),
                }
                if not entry["httpApiId"]:
                    continue
                fallback.append(entry)
                if gateway_id and dc.get("gatewayId") != gateway_id:
                    continue
                result.append(entry)
                break
    return result or fallback


def has_group_api_authorization(consumer_group_id: str, api_id: str) -> bool:
    """判断消费者组对指定 AI API 是否已有组级授权规则。

    组级授权对组内所有成员生效——成员只需入组即可调用，无需单独授权。
    """
    for item in query_authorization_rules(consumer_group_id=consumer_group_id):
        api_info = item.get("apiInfo") or {}
        if api_info.get("httpApiId") == api_id:
            return True
    return False


def ensure_consumer_api_authorization(
    client: Any,
    consumer_id: str,
    gateway_id: str,
    *,
    api_id: str | None = None,
) -> dict[str, Any] | None:
    """确保消费者对目标 LLM API 有授权规则，缺失时创建。返回创建结果或 None（已有授权）。

    client 为 manager.AliyunApigClient。api_id 为空时自动发现网关下第一个 LLM API。
    """
    if api_id is None:
        llm_apis = list_llm_apis(gateway_id=gateway_id)
        if not llm_apis:
            raise RuntimeError(f"网关 {gateway_id} 下未发现可授权的 LLM API")
        api_id = llm_apis[0]["httpApiId"]
    if has_api_authorization(consumer_id, api_id):
        return None
    # environmentId 从 LLM API 的部署配置推断
    env_id: str | None = None
    for item in list_llm_apis(gateway_id=gateway_id):
        if item["httpApiId"] == api_id:
            env_id = item.get("environmentId")
            break
    if not env_id:
        raise RuntimeError(f"LLM API {api_id} 未找到部署环境（environmentId）")
    return create_authorization_rule(
        consumer_id, api_id=api_id, environment_id=env_id,
    )


def ensure_group_member_authorized(
    client: Any,
    consumer_id: str,
    consumer_group_id: str,
    gateway_id: str,
    *,
    api_id: str | None = None,
) -> dict[str, Any] | None:
    """确保入组消费者可调用 LLM API：组有授权则依赖组授权（返回 None），
    仅当组没有任何对目标 API 的授权时才为消费者单独兜底建规则。

    实测（2026-09-03）：组级授权对组内成员生效，成员无需单独授权。
    """
    if api_id is None:
        llm_apis = list_llm_apis(gateway_id=gateway_id)
        if not llm_apis:
            raise RuntimeError(f"网关 {gateway_id} 下未发现可授权的 LLM API")
        api_id = llm_apis[0]["httpApiId"]
    if has_group_api_authorization(consumer_group_id, api_id):
        return None
    return ensure_consumer_api_authorization(
        client, consumer_id, gateway_id, api_id=api_id,
    )
