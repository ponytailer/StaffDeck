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

    identityConfig 必填：api_key 非空时用自定义凭证（Custom 模式），
    否则用 Auto 模式由系统生成（真实 API 校验 identityConfig 不能为空）。
    """
    body: dict[str, Any] = {
        "name": name,
        "description": description,
        "enable": enable,
        "gatewayType": gateway_type,
    }
    # 实测（2026-09）：真实云端 apikeySource.source 仅接受 Default/Header，
    # 传 Authorization 会 400 InvalidParameter.WithValue；且 identityConfig 必填。
    # Custom 模式由调用方（本系统）生成强随机 Key 写入，明文在创建时即持有。
    credentials: list[dict[str, Any]] = (
        [{"generateMode": "Custom", "apikey": api_key}]
        if api_key
        else [{"generateMode": "System"}]
    )
    body["apiKeyIdentityConfig"] = {
        "type": "Apikey",
        "apiKeySources": [
            {"source": "Default", "value": "Authorization", "credentials": credentials}
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
    name: str | None = None,
    description: str | None = None,
    api_key: str | None = None,
    enable: bool | None = None,
    consumer_group_ids: list[str] | None = None,
    keep_credentials: list[str] | None = None,
) -> dict[str, Any]:
    """更新消费者。api_key 非空时将其作为自定义凭证追加到该消费者。

    用于审批分配 API Key:把申请的自定义 Key 以 Custom 模式写入组内消费者,
    使同一消费组(消费者)下的多个成员共享该消费者与对应配额规则。

    阿里云 UpdateConsumer 支持修改 name/description/enable(已实测)。
    consumer_group_ids 非空时把消费者加入指定消费组(csg- 列表,全量覆盖,
    传空列表会把消费者移出所有组)。
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
        body["apiKeyIdentityConfig"] = {
            "type": "Apikey",
            "apiKeySources": [
                {
                    "source": "Default",
                    "value": "Authorization",
                    "credentials": [{"generateMode": "Custom", "apikey": api_key}],
                }
            ],
        }
    elif keep_credentials is not None:
        # 凭证裁剪：仅保留 keep_credentials 中的 Key（按明文匹配 Custom 凭证）。
        # mock 模式：_mock_credential_replace 标记 mock 分支走"全量覆盖"而非"追加"。
        body["apiKeyIdentityConfig"] = {
            "type": "Apikey",
            "apiKeySources": [
                {
                    "source": "Default",
                    "value": "Authorization",
                    "credentials": [
                        {"generateMode": "Custom", "apikey": ak}
                        for ak in keep_credentials
                    ],
                }
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
