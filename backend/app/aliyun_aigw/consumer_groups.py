"""消费者组（ConsumerGroup）管理业务方法。

阿里云 AI Gateway 消费者组接口：
- ListConsumerGroups  GET    /v1/consumer-groups
- GetConsumerGroup    GET    /v1/consumer-groups/{consumerGroupId}
- ListConsumerGroupConsumers  GET /v1/consumer-groups/{consumerGroupId}/consumers
"""
from __future__ import annotations

from typing import Any

from .client import _request


def list_consumer_groups(
    *,
    gateway_type: str = "AI",
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """列出消费者组。"""
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = _request(
            "GET",
            "/v1/consumer-groups",
            action="ListConsumerGroups",
            query={"gatewayType": gateway_type, "pageNumber": page, "pageSize": page_size},
        )
        data = resp.get("data") or {}
        batch = data.get("items") if isinstance(data, dict) else data
        if not batch:
            break
        items.extend(batch)
        total = data.get("totalSize") if isinstance(data, dict) else None
        if total is None or len(items) >= int(total):
            break
    return items


def get_consumer_group(consumer_group_id: str) -> dict[str, Any]:
    """查询单个消费者组详情。"""
    resp = _request(
        "GET",
        f"/v1/consumer-groups/{consumer_group_id}",
        action="GetConsumerGroup",
    )
    return resp.get("data") or resp


def list_consumer_group_consumers(
    consumer_group_id: str,
    *,
    page_size: int = 100,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """列出消费者组内的消费者成员。"""
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = _request(
            "GET",
            f"/v1/consumer-groups/{consumer_group_id}/consumers",
            action="ListConsumerGroupConsumers",
            query={"pageNumber": page, "pageSize": page_size},
        )
        data = resp.get("data") or {}
        batch = data.get("items") if isinstance(data, dict) else data
        if not batch:
            break
        items.extend(batch)
        total = data.get("totalSize") if isinstance(data, dict) else None
        if total is None or len(items) >= int(total):
            break
    return items


def add_consumers_to_group(
    consumer_group_id: str,
    consumer_ids: list[str],
) -> dict[str, Any]:
    """批量把消费者加入消费者组（BatchAddConsumerGroupConsumers）。

    端点：POST /v1/consumer-groups/{consumerGroupId}/consumers/batch-add
    返回 data: {successConsumerIds, skippedConsumerIds, failedConsumerIds}
    """
    resp = _request(
        "POST",
        f"/v1/consumer-groups/{consumer_group_id}/consumers/batch-add",
        action="BatchAddConsumerGroupConsumers",
        body={"consumerIds": consumer_ids},
    )
    return (resp.get("data") or {}) if isinstance(resp, dict) else {}