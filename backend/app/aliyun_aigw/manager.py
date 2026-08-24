"""高层客户端适配层（供审批分配 API Key 使用）。

对外暴露与历史 `clients.aliyun_apig.AliyunApigClient` 一致的接口：
    client.create_consumer(name, api_key, ...)
    client.add_consumer_quota_rule(gateway_id, consumer_ids, quota_limit, period_type, ...)
    get_apig_client()   # mock 模式(无 AK/SK)自动返回 mock 客户端

底层请求由 client._request 统一走签名或 mock 分支，调用方无感知。
"""
from __future__ import annotations

from typing import Any

from . import config
from . import consumers
from . import quota


class AliyunApigError(Exception):
    """阿里云 APIG 调用失败。"""


class AliyunApigClient:
    """阿里云 APIG(云原生 API 网关 / AI 网关) 客户端。"""

    def __init__(
        self,
        access_key_id: str,
        access_key_secret: str,
        region: str = config.REGION,
    ):
        self.access_key_id = access_key_id
        self.access_key_secret = access_key_secret
        self.region = region

    def create_consumer(
        self,
        name: str,
        api_key: str | None = None,
        description: str | None = None,
        gateway_type: str = "AI",
    ) -> str:
        """创建消费者并配置自定义 API Key 凭证。返回 consumerId。"""
        resp = consumers.create_consumer(
            name=name,
            api_key=api_key,
            description=description or "",
            gateway_type=gateway_type,
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        consumer_id = data.get("consumerId") if isinstance(data, dict) else None
        if not consumer_id:
            raise AliyunApigError(f"创建消费者未返回 consumerId：{resp}")
        return consumer_id

    def add_consumer_quota_rule(
        self,
        gateway_id: str,
        consumer_ids: list[str],
        quota_limit: int,
        period_type: str,
        rule_name: str | None = None,
        timezone: str = "UTC+8",
    ) -> str:
        """为消费者创建 FinOps 配额规则(token 维度)。先 dryRun 预检, 再正式提交。返回 ruleId。"""
        preview = quota.create_quota_rule(
            rule_name or "quota-rule",
            "token",
            quota_limit,
            gateway_id=gateway_id,
            period_type=period_type,
            timezone=timezone,
            consumer_ids=consumer_ids,
            dry_run=True,
        )
        preview_data = preview.get("data", preview) if isinstance(preview, dict) else preview
        if isinstance(preview_data, dict) and preview_data.get("accepted") is False:
            conflict = preview_data.get("conflictPreview", {})
            raise AliyunApigError(f"配额规则冲突，无法创建：{conflict}")
        committed = quota.create_quota_rule(
            rule_name or "quota-rule",
            "token",
            quota_limit,
            gateway_id=gateway_id,
            period_type=period_type,
            timezone=timezone,
            consumer_ids=consumer_ids,
            dry_run=False,
        )
        committed_data = committed.get("data", committed) if isinstance(committed, dict) else committed
        rule_id = committed_data.get("ruleId") if isinstance(committed_data, dict) else None
        if not rule_id:
            raise AliyunApigError(f"创建配额规则未返回 ruleId：{committed}")
        return rule_id

    def update_consumer_quota_rule(
        self,
        gateway_id: str,
        rule_id: str,
        quota_limit: int,
    ) -> None:
        """更新已有配额规则的额度（UpdateGatewayQuotaRule）。"""
        quota.update_quota_rule(
            rule_id,
            gateway_id=gateway_id,
            quota_limit=quota_limit,
        )

    def add_consumer_credential(
        self,
        consumer_id: str,
        api_key: str,
    ) -> None:
        """向已有消费者追加一个自定义 API Key 凭证（UpdateConsumer）。"""
        consumers.update_consumer(consumer_id, api_key=api_key)

    def attach_consumer_to_rule(
        self,
        gateway_id: str,
        rule_id: str,
        consumer_id: str,
    ) -> None:
        """把消费者加入配额规则的限流范围（UpdateGatewayQuotaRule addIds）。"""
        quota.update_quota_rule(rule_id, gateway_id=gateway_id, add_ids=[consumer_id])

    def detach_consumer_from_rule(
        self,
        gateway_id: str,
        rule_id: str,
        consumer_id: str,
    ) -> None:
        """把消费者移出配额规则（UpdateGatewayQuotaRule removeIds）。"""
        quota.update_quota_rule(rule_id, gateway_id=gateway_id, remove_ids=[consumer_id])

    def update_quota_rule_meta(
        self,
        gateway_id: str,
        rule_id: str,
        rule_name: str | None = None,
        period_type: str | None = None,
    ) -> None:
        """更新配额规则元信息（名称/周期）。"""
        quota.update_quota_rule(
            rule_id,
            gateway_id=gateway_id,
            rule_name=rule_name,
            period_type=period_type,
        )

    def update_consumer(
        self,
        consumer_id: str,
        description: str | None = None,
        enable: bool | None = None,
    ) -> None:
        """更新消费者元信息（UpdateConsumer，目前仅支持 description / enable）。"""
        consumers.update_consumer(consumer_id, description=description, enable=enable)

    def delete_consumer(self, consumer_id: str) -> None:
        """删除消费者（DeleteConsumer）。"""
        consumers.delete_consumer(consumer_id)

    def delete_quota_rule(self, rule_id: str, *, gateway_id: str | None = None) -> None:
        """删除配额规则（DeleteGatewayQuotaRule）。"""
        quota.delete_quota_rule(rule_id, gateway_id=gateway_id)

    def get_consumer_quota_usage(
        self,
        gateway_id: str,
        rule_id: str,
        consumer_id: str,
    ) -> dict[str, Any]:
        """查询某消费者在配额规则下的用量（GetGatewayQuotaRuleSubjectUsage）。"""
        return quota.get_quota_usage(
            subject_id=consumer_id,
            gateway_id=gateway_id,
            rule_id=rule_id,
        )


def get_apig_client() -> AliyunApigClient | None:
    """构造客户端。

    - mock 模式（USE_MOCK）：即使未配置 AK/SK 也返回客户端，走 mock 数据。
    - 生产模式且配置了 AK/SK：返回真实客户端。
    - 生产模式且缺 AK/SK：返回 None（调用方应据此提示配置缺失）。
    """
    ak = config.ACCESS_KEY_ID
    sk = config.ACCESS_KEY_SECRET
    if config.USE_MOCK:
        return AliyunApigClient(
            access_key_id=ak or "mock-ak",
            access_key_secret=sk or "mock-sk",
            region=config.REGION,
        )
    if not (ak and sk):
        return None
    return AliyunApigClient(ak, sk, config.REGION)
