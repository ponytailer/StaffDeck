"""消费者（Consumer）管理业务方法。

已对接接口：
- CreateConsumer   POST   /v1/consumers
- DeleteConsumer   DELETE /v1/consumers/{consumerId}
- ListConsumers    GET    /v1/consumers

CreateConsumer 支持传入自定义 API Key（credentials.generateMode=Custom），
由系统生成强随机串直接持有明文返回给申请人。

授权模型（2026-09 实测）：
- 消费者凭证与「消费者→AI API 授权规则」是两回事。创建消费者后必须再创建
  授权规则（CreateConsumerAuthorizationRules，resourceType=LLM + environmentId），
  否则调用网关即使凭证正确也返回 401。
- 查询授权规则用 QueryConsumerAuthorizationRules（GET /v1/authorization-rules）。
"""
from __future__ import annotations

from typing import Any

from .client import _request


def create_consumer(
    name: str,
    *,
    description: str = "",
    enable: bool = True,
    gateway_type: str = "AI",
    api_key: str | None = None,
) -> dict[str, Any]:
    """创建消费者。gateway_type=AI 表示 AI 网关消费者。

    凭证方式固定为 API Key，来源 value="Authorization"（即 Authorization: Bearer <token>）。
    api_key 非空：generateMode=Custom，写入服务端生成的 Key（本系统持有明文，推荐——
    实测云端 System 模式 GetConsumer 不回带明文）；
    api_key 为空：generateMode=System 由云端生成。
    """
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "enable": enable,
        "gatewayType": gateway_type,
    }
    # 实测（2026-09）：真实云端 apikeySource.source 仅接受 Default/Header；
    # value="Authorization" 即标准 Authorization: Bearer <token> 头。
    # 【关键实测结论】credentials 必须挂在 apiKeyIdentityConfig 顶层，
    # 嵌在 apiKeySources[].credentials 里会被云端静默丢弃（创建返回 Ok 但 Key 无法鉴权 401）。
    credentials: list[dict[str, Any]] = (
        [{"generateMode": "Custom", "apikey": api_key}]
        if api_key
        else [{"generateMode": "System"}]
    )
    body["apiKeyIdentityConfig"] = {
        "type": "Apikey",
        "apikeySource": {"source": "Default", "value": "Authorization"},
        "credentials": credentials,
    }
    return _request("POST", "/v1/consumers", action="CreateConsumer", body=body)


def delete_consumer(consumer_id: str) -> dict[str, Any]:
    """删除消费者。"""
    # 实测：DELETE 成功时响应体可能为空（resp.json() 返回 None），此处不做结构断言
    return _request(
        "DELETE",
        f"/v1/consumers/{consumer_id}",
        action="DeleteConsumer",
    )


def update_consumer(
    consumer_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    api_key: str | None = None,
    enable: bool | None = None,
    consumer_group_ids: list[str] | None = None,
    keep_credentials: list[str] | None = None,
) -> dict[str, Any]:
    """更新消费者。

    阿里云 UpdateConsumer 支持修改 name/description/enable(已实测)。
    consumer_group_ids 非空时把消费者加入指定消费组(csg- 列表,全量覆盖,
    传空列表会把消费者移出所有组)。
    api_key 非空时将其作为服务端生成的 Custom 凭证覆盖写入。
    keep_credentials 非空时执行"凭证裁剪":按剩余凭证全量覆盖 apiKeySources
    (云侧无 DeleteConsumerApiKey API,删除单凭证只能覆盖写)。
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if enable is not None:
        body["enable"] = enable
    if consumer_group_ids is not None:
        body["consumerGroupIds"] = consumer_group_ids
    if api_key:
        # 【关键实测结论】credentials 必须挂在 apiKeyIdentityConfig 顶层（同 create_consumer），
        # 嵌在 apiKeySources[].credentials 里会被云端静默丢弃（返回 Ok 但 Key 无法鉴权 401）。
        body["apiKeyIdentityConfig"] = {
            "type": "Apikey",
            "apikeySource": {"source": "Default", "value": "Authorization"},
            "credentials": [{"generateMode": "Custom", "apikey": api_key}],
        }
    elif keep_credentials is not None:
        # 凭证裁剪：仅保留 keep_credentials 中的 Key（覆盖写剩余凭证）。
        # mock 模式：_mock_credential_replace 标记 mock 分支走"全量覆盖"而非"追加"。
        body["apiKeyIdentityConfig"] = {
            "type": "Apikey",
            "apikeySource": {"source": "Default", "value": "Authorization"},
            "credentials": [
                {"generateMode": "Custom", "apikey": ak} for ak in keep_credentials
            ],
            "_mock_credential_replace": True,
        }
    return _request("PUT", f"/v1/consumers/{consumer_id}", action="UpdateConsumer", body=body)


def list_consumers(
    *,
    gateway_type: str = "AI",
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """列出消费者（用于管理页），返回 data.items 列表。

    注意：阿里云 ListConsumers 必须显式传 gatewayType，否则返回空列表
    （AI 网关消费者 gatewayType=AI；不传时接口默认查普通网关消费者，totalSize=0）。
    """
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = _request(
            "GET",
            "/v1/consumers",
            action="ListConsumers",
            query={"gatewayType": gateway_type, "pageNumber": page, "pageSize": page_size},
        )
        # ListConsumers 返回 {code, data: {items: [...], totalSize}, ...}
        data = resp.get("data") or {}
        batch = data.get("items") if isinstance(data, dict) else data
        if not batch:
            break
        items.extend(batch)
        total = data.get("totalSize") if isinstance(data, dict) else None
        if total is None or len(items) >= int(total):
            break
    return items


def get_consumer(consumer_id: str) -> dict[str, Any]:
    """查询单个消费者详情（GetConsumer）。"""
    resp = _request("GET", f"/v1/consumers/{consumer_id}", action="GetConsumer")
    return resp.get("data") or resp
