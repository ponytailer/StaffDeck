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
from . import authorization_rules
from . import consumer_groups
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
        """创建消费者（API Key 凭证，来源 Authorization: Bearer）。返回 consumerId。

        api_key 非空（推荐）：Custom 模式写入服务端生成的 Key，本系统持有明文
        （实测云端 System 模式 GetConsumer 不回带明文，需要明文的场景必须服务端生成）。
        api_key 为空：System 模式由云端生成。
        """
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

    def add_consumers_to_group(
        self,
        consumer_group_id: str,
        consumer_ids: list[str],
    ) -> dict[str, Any]:
        """批量把消费者加入消费者组（BatchAddConsumerGroupConsumers）。

        返回 {successConsumerIds, skippedConsumerIds, failedConsumerIds}；
        failedConsumerIds 非空时抛 AliyunApigError。
        """
        result = consumer_groups.add_consumers_to_group(consumer_group_id, consumer_ids)
        failed = result.get("failedConsumerIds") or []
        if failed:
            raise AliyunApigError(f"消费者加入消费组失败：{failed}")
        return result

    def add_consumer_quota_rule(
        self,
        gateway_id: str,
        consumer_ids: list[str],
        quota_limit: int,
        period_type: str,
        rule_name: str | None = None,
        timezone: str = "UTC+8",
        quota_dimension: str = "credit",
        consumer_group_ids: list[str] | None = None,
    ) -> str:
        """创建 FinOps 配额规则。先 dryRun 预检, 再正式提交。返回 ruleId。

        两种主体粒度（互斥）：
        - consumer_ids：消费者粒度（cs- 列表）
        - consumer_group_ids：消费组粒度（csg- 列表，AI 网关 2.1.21+ 支持，
          整组共享规则限额，组成员入组即生效，无需逐个绑定）

        quota_dimension 必须在 token / credit 之间显式选择并透传到阿里云
        （AI 网关现网规则使用的维度为 credit，此前硬编码 token 会导致维度不符）。
        """
        if consumer_ids and consumer_group_ids:
            raise AliyunApigError("consumer_ids 与 consumer_group_ids 互斥，不能同时传入")
        group_mode = bool(consumer_group_ids)
        preview = quota.create_quota_rule(
            rule_name or "quota-rule",
            quota_dimension,
            quota_limit,
            gateway_id=gateway_id,
            period_type=period_type,
            timezone=timezone,
            consumer_ids=None if group_mode else consumer_ids,
            consumer_group_ids=consumer_group_ids,
            dry_run=True,
        )
        preview_data = preview.get("data", preview) if isinstance(preview, dict) else preview
        if isinstance(preview_data, dict) and preview_data.get("accepted") is False:
            conflict = preview_data.get("conflictPreview", {})
            raise AliyunApigError(f"配额规则冲突，无法创建：{conflict}")
        committed = quota.create_quota_rule(
            rule_name or "quota-rule",
            quota_dimension,
            quota_limit,
            gateway_id=gateway_id,
            period_type=period_type,
            timezone=timezone,
            consumer_ids=None if group_mode else consumer_ids,
            consumer_group_ids=consumer_group_ids,
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
        """为已有消费者设置/轮换 API Key 凭证（UpdateConsumer 覆盖写）。"""
        consumers.update_consumer(consumer_id, api_key=api_key)

    def authorize_consumer_api(
        self,
        consumer_id: str,
        api_id: str,
        environment_id: str,
    ) -> dict[str, Any]:
        """建立「消费者→AI API」授权规则（CreateConsumerAuthorizationRules）。

        实测：仅创建消费者+凭证而缺授权规则时，调用网关一律 401。
        """
        return authorization_rules.create_authorization_rule(
            consumer_id, api_id=api_id, environment_id=environment_id,
        )

    def ensure_consumer_api_authorization(
        self,
        consumer_id: str,
        gateway_id: str,
        api_id: str | None = None,
    ) -> dict[str, Any] | None:
        """确保消费者对 LLM API 有授权规则（缺失时创建；返回 None 表示已有授权）。

        api_id 为空时自动发现网关下第一个 LLM API。
        """
        return authorization_rules.ensure_consumer_api_authorization(
            self, consumer_id, gateway_id, api_id=api_id,
        )

    def has_api_authorization(self, consumer_id: str, api_id: str) -> bool:
        """判断消费者对指定 AI API 是否已有授权规则。"""
        return authorization_rules.has_api_authorization(consumer_id, api_id)

    def ensure_group_member_authorized(
        self,
        consumer_id: str,
        consumer_group_id: str,
        gateway_id: str,
        api_id: str | None = None,
    ) -> dict[str, Any] | None:
        """确保入组消费者可调用 LLM API。

        组已有授权规则时直接依赖组授权（返回 None）；否则为消费者单独兜底建规则。
        """
        return authorization_rules.ensure_group_member_authorized(
            self, consumer_id, consumer_group_id, gateway_id, api_id=api_id,
        )

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

    def attach_consumer_group_to_rule(
        self,
        gateway_id: str,
        rule_id: str,
        consumer_group_id: str,
    ) -> None:
        """把消费组加入配额规则限流范围（UpdateGatewayQuotaRule addIds 传组 ID）。

        消费组粒度配额（网关 2.1.21+）：整组共享规则限额，组成员入组即生效。
        """
        quota.update_quota_rule(rule_id, gateway_id=gateway_id, add_group_ids=[consumer_group_id])

    def detach_consumer_group_from_rule(
        self,
        gateway_id: str,
        rule_id: str,
        consumer_group_id: str,
    ) -> None:
        """把消费组移出配额规则（UpdateGatewayQuotaRule removeIds 传组 ID）。"""
        quota.update_quota_rule(rule_id, gateway_id=gateway_id, remove_group_ids=[consumer_group_id])

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
        name: str | None = None,
        description: str | None = None,
        enable: bool | None = None,
        consumer_group_ids: list[str] | None = None,
        keep_credentials: list[str] | None = None,
    ) -> None:
        """更新消费者元信息（UpdateConsumer，支持 name / description / enable）。

        consumer_group_ids 非空时把消费者加入指定消费组（云端 csg- 列表，全量覆盖）。
        keep_credentials 非空时执行凭证裁剪（仅保留给定 Key）。
        """
        consumers.update_consumer(
            consumer_id,
            name=name,
            description=description,
            enable=enable,
            consumer_group_ids=consumer_group_ids,
            keep_credentials=keep_credentials,
        )

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


    # ---------- 只读查询（实时直读阿里云） ----------

    def list_consumers(self, gateway_type: str = "AI") -> list[dict[str, Any]]:
        """列出消费者（gatewayType 必传，否则阿里云返回空列表）。"""
        return consumers.list_consumers(gateway_type=gateway_type)

    def get_consumer(self, consumer_id: str) -> dict[str, Any]:
        """查询单个消费者详情。云端返回 success=false（如不存在）时抛 AliyunApigError。"""
        resp = consumers.get_consumer(consumer_id)
        if isinstance(resp, dict) and resp.get("success") is False:
            raise AliyunApigError(
                f"消费者 {consumer_id} 不存在或不可访问：{resp.get('code') or resp.get('message')}"
            )
        return resp.get("data") or resp

    def list_consumer_groups(self, gateway_type: str = "AI") -> list[dict[str, Any]]:
        """列出消费者组。"""
        return consumer_groups.list_consumer_groups(gateway_type=gateway_type)

    def get_consumer_group(self, consumer_group_id: str) -> dict[str, Any]:
        """查询单个消费者组详情。"""
        resp = consumer_groups.get_consumer_group(consumer_group_id)
        if isinstance(resp, dict) and resp.get("success") is False:
            raise AliyunApigError(
                f"消费者组 {consumer_group_id} 不存在或不可访问：{resp.get('code') or resp.get('message')}"
            )
        return resp.get("data") or resp

    def list_consumer_group_consumers(
        self, consumer_group_id: str
    ) -> list[dict[str, Any]]:
        """列出消费者组内的消费者成员。"""
        return consumer_groups.list_consumer_group_consumers(consumer_group_id)

    def list_quota_rules(self, gateway_id: str | None = None) -> list[dict[str, Any]]:
        """列出网关下的配额规则。"""
        return quota.list_quota_rules(gateway_id=gateway_id)

    def get_quota_rule(self, rule_id: str, *, gateway_id: str | None = None) -> dict[str, Any]:
        """查询单条配额规则详情。"""
        return quota.get_quota_rule(rule_id, gateway_id=gateway_id)

    def list_quota_rule_subjects(
        self, rule_id: str, *, gateway_id: str | None = None
    ) -> list[dict[str, Any]]:
        """列出配额规则绑定的消费者主体（含用量）。"""
        return quota.list_quota_rule_subjects(rule_id, gateway_id=gateway_id)


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
