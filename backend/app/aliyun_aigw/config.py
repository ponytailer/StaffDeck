"""阿里云 AI Gateway 客户端配置（环境变量 / .env）。

Mock / 生产切换（重要）
============================================================================
为方便本地演示（尚无真实 AK/SK 或网关资源），默认在「未配置 AK/SK」或
环境变量 AIGW_USE_MOCK=1 时走 mock 数据分支，返回结构与真实接口一致，
可直接驱动图表与列表展示。

上生产时：
  1. 设置 AIGW_USE_MOCK=0（或直接删除该变量）
  2. 配置真实凭据：ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET
  3. 配置网关与配额规则 ID：ALIYUN_APIG_GATEWAY_ID / ALIYUN_APIG_QUOTA_RULE_ID
     （以及可选的 ALIYUN_APIG_SUBJECT_ID 作为默认查询主体）
  4. 多网关场景（审批分配 API Key 用）：用 ALIYUN_APIG_GATEWAYS 提供网关列表

网关配置两种方式（任选其一）
----------------------------------------------------------------------------
A. 多网关列表（推荐，支持审批时下拉选择）：
     ALIYUN_APIG_GATEWAYS='[{"name":"主力网关","gateway_id":"gw-xxx",
       "gateway_url":"https://xxx.aliyuncs.com/v1","region":"cn-hangzhou"}]'
B. 单网关（兼容旧用法）：ALIYUN_APIG_GATEWAY_ID + ALIYUN_APIG_HOST
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

API_VERSION = "2024-03-27"
ALGORITHM = "ACS3-HMAC-SHA256"

# 从 .env 加载配置（已存在的环境变量优先，不覆盖）
load_dotenv(override=False)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


ACCESS_KEY_ID = _env("ALIYUN_ACCESS_KEY_ID")
ACCESS_KEY_SECRET = _env("ALIYUN_ACCESS_KEY_SECRET")
REGION = _env("ALIYUN_REGION", "cn-hangzhou")
# AI Gateway 控制面域名，不同账号/环境可能不同，用 ALIYUN_APIG_HOST 覆盖
HOST = _env("ALIYUN_APIG_HOST", f"apig.{REGION}.aliyuncs.com")
GATEWAY_ID = _env("ALIYUN_APIG_GATEWAY_ID")
QUOTA_RULE_ID = _env("ALIYUN_APIG_QUOTA_RULE_ID")
DEFAULT_SUBJECT_ID = _env("ALIYUN_APIG_SUBJECT_ID")

# Mock 开关：显式置 1，或「未配置 AK/SK」时自动 mock（避免无凭据直接报错）
USE_MOCK = _env("AIGW_USE_MOCK", "0") == "1" or not (ACCESS_KEY_ID and ACCESS_KEY_SECRET)

if USE_MOCK:
    print(
        "[aigw] 使用 MOCK 数据（未配置 AK/SK 或 AIGW_USE_MOCK=1）。"
        "上生产请配置凭据并设 AIGW_USE_MOCK=0。"
    )


@dataclass
class GatewayConfig:
    """单个 AI 网关的连接配置。"""

    name: str
    gateway_id: str
    gateway_url: str
    region: str = REGION


def get_gateway_configs() -> list[GatewayConfig]:
    """读取网关列表（调用时读取环境变量，便于测试注入）。

    优先级：
      1. ALIYUN_APIG_GATEWAYS（JSON 数组，多网关）
      2. ALIYUN_APIG_GATEWAY_ID（单网关，用 HOST 拼出地址）
      3. mock 模式：返回示例网关，便于本地演示
    """
    raw = _env("ALIYUN_APIG_GATEWAYS")
    configs: list[GatewayConfig] = []
    if raw:
        try:
            data = __import__("json").loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                gateway_id = item.get("gateway_id") or item.get("gatewayId")
                gateway_url = item.get("gateway_url") or item.get("gatewayUrl")
                if not (name and gateway_id and gateway_url):
                    continue
                configs.append(
                    GatewayConfig(
                        name=str(name),
                        gateway_id=str(gateway_id),
                        gateway_url=str(gateway_url),
                        region=str(item.get("region") or REGION),
                    )
                )
    if configs:
        return configs

    if GATEWAY_ID:
        return [
            GatewayConfig(
                name="默认网关",
                gateway_id=GATEWAY_ID,
                gateway_url=f"https://{HOST}/v1",
                region=REGION,
            )
        ]

    if USE_MOCK:
        return [
            GatewayConfig(
                name="主力网关(演示)",
                gateway_id="gw-mock-001",
                gateway_url="https://apig-gw-mock-001.aliyuncs.com/v1",
                region=REGION,
            ),
            GatewayConfig(
                name="备用网关(演示)",
                gateway_id="gw-mock-002",
                gateway_url="https://apig-gw-mock-002.aliyuncs.com/v1",
                region=REGION,
            ),
        ]
    return []


def get_gateway_config(name: str) -> GatewayConfig | None:
    for cfg in get_gateway_configs():
        if cfg.name == name:
            return cfg
    return None
