"""消费者（Consumer）管理业务方法。

已对接接口：
- CreateConsumer   POST   /v1/consumers
- DeleteConsumer   DELETE /v1/consumers/{consumerId}
- ListConsumers    GET    /v1/consumers

CreateConsumer 支持传入自定义 API Key（apikeyIdentityConfig.generateMode=Custom），
由系统生成强随机串直接持有明文返回给申请人。
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

    api_key 非空时，使用自定义凭证（Custom 模式），系统生成的明文 Key 直接写入。
    """
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "enable": enable,
        "gatewayType": gateway_type,
    }
    if api_key:
        body["apikeyIdentityConfig"] = {
            "type": "Apikey",
            "apikeySources": [
                {
                    "source": "Authorization",
                    "value": "Authorization",
                    "credentials": [{"generateMode": "Custom", "apikey": api_key}],
                }
            ],
        }
    return _request("POST", "/v1/consumers", action="CreateConsumer", body=body)


def delete_consumer(consumer_id: str) -> dict[str, Any]:
    """删除消费者。"""
    return _request(
        "DELETE",
        f"/v1/consumers/{consumer_id}",
        action="DeleteConsumer",
    )


def update_consumer(
    consumer_id: str,
    *,
    description: str | None = None,
    api_key: str | None = None,
    enable: bool | None = None,
) -> dict[str, Any]:
    """更新消费者。api_key 非空时将其作为自定义凭证追加到该消费者。

    用于审批分配 API Key:把申请的自定义 Key 以 Custom 模式写入组内消费者,
    使同一消费组(消费者)下的多个成员共享该消费者与对应配额规则。
    """
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if enable is not None:
        body["enable"] = enable
    if api_key:
        body["apikeyIdentityConfig"] = {
            "type": "Apikey",
            "apikeySources": [
                {
                    "source": "Authorization",
                    "value": "Authorization",
                    "credentials": [{"generateMode": "Custom", "apikey": api_key}],
                }
            ],
        }
    return _request("PUT", f"/v1/consumers/{consumer_id}", action="UpdateConsumer", body=body)


def list_consumers() -> list[dict[str, Any]]:
    """列出消费者（用于管理页），返回 data 列表。"""
    resp = _request("GET", "/v1/consumers", action="ListConsumers")
    # ListConsumers 返回 {code, data: {items: [...]}, ...}
    data = resp.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    return items or []
