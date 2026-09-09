"""知识路由决策缓存（cache-aside，Redis 可选底座）。

缓存 LLM 路由（knowledge.document_route / knowledge.bucket_route）的决策结果：
(key) = tenant + agent + 授权知识库集合 + 归一化查询 → (value) = 选中的 ID 列表。

- 命中：跳过一次 10-25s 的 LLM 路由调用（deepseek-v4-flash 实测 document_route
  2s、bucket_route 12-74s）；重复问法（含「怎么/如何/我想知道」等噪声词差异）
  经 query_norm 归一后共享同一 key。
- 失效：知识库写路径（上传/更新/入库完成）按 (tenant, kb_id) 模式删除；
  TTL 30 分钟兜底，漏失效时最长 30 分钟后自动新鲜。
- Redis 不可用 → 全部函数静默降级，行为与无缓存一致（get 返回 None）。
- 只缓存 LLM 的「选择决策」，不缓存词法结果——语义纠偏能力保留。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.object_cache import load_json, store_json, invalidate_namespace_pattern

NAMESPACE = "kroute"
ROUTE_CACHE_TTL_SECONDS = 1800

# 查询噪声词：纯提问方式差异，不影响路由语义。归一时剔除以聚合相似问法。
# 扩充自 app/knowledge/service.py QUERY_NOISE_PHRASES（那是打分用，这里是 key 归一）。
_ROUTE_NOISE_PHRASES = (
    "我想知道",
    "麻烦帮我",
    "请问",
    "帮我",
    "我想",
    "我要",
    "需要",
    "帮忙",
    "告诉我",
    "一下",
    "怎么",
    "如何",
    "怎样",
    "什么",
    "哪些",
    "哪个",
    "么样",  # 「怎么样」残片
)


def query_norm(query: str) -> str:
    """查询归一：去标点空白、去提问噪声词、小写。

    例：「如何申领电脑」「怎么申领电脑」「我想知道如何申领电脑？」「申领电脑」
    归一后同为「申领电脑」。
    """

    text = (query or "").strip().lower()
    # 去标点与空白
    text = re.sub(r"[？?！!。.，,、\s：:；;]+", "", text)
    # 去噪声词（多轮替换处理相邻组合）
    for _ in range(2):
        for phrase in _ROUTE_NOISE_PHRASES:
            text = text.replace(phrase, "")
    return text.strip()


def route_cache_key(
    tenant_id: str,
    agent_id: str | None,
    knowledge_base_ids: list[str] | None,
    query: str,
) -> str | None:
    """构造路由缓存 key；无法构造时返回 None（调用方跳过缓存）。"""

    tenant = (tenant_id or "").strip()
    if not tenant or not (query or "").strip():
        return None
    agent = (agent_id or "").strip() or "-"
    kb_part = ",".join(sorted({str(kb).strip() for kb in (knowledge_base_ids or []) if str(kb).strip()}))
    normalized = query_norm(query)
    if not normalized:
        return None
    query_hash = hashlib.md5(f"{kb_part}|{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"{tenant}:{agent}:{query_hash}"


def get_route_cache(key: str | None) -> dict[str, Any] | None:
    """读路由决策缓存；未命中/不可用返回 None。"""

    if not key:
        return None
    data = load_json(key, namespace=NAMESPACE)
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("document_ids"), list) or not isinstance(data.get("bucket_ids"), list):
        return None
    return data


def store_route_cache(
    key: str | None,
    document_ids: list[str],
    bucket_ids: list[str],
    ttl_seconds: int = ROUTE_CACHE_TTL_SECONDS,
) -> bool:
    """写路由决策缓存；返回是否真正写入（Redis 不可用时返回 False）。"""

    if not key:
        return False
    return store_json(
        key,
        {"document_ids": list(document_ids), "bucket_ids": list(bucket_ids)},
        ttl_seconds=ttl_seconds,
        namespace=NAMESPACE,
    )


def invalidate_knowledge_base(tenant_id: str, knowledge_base_ids: list[str] | str) -> None:
    """知识库内容变更时，删除该库相关的全部路由缓存。

    key 中 query_hash 含 kb_ids 排序串，因此按「tenant:agent:*」模式扫描后
    在应用侧过滤——SCAN 模式无法反向匹配 query_hash 里的 kb 组合，
    采用 tenant + agent 前缀扫描、解析 value 校对 kb_ids。

    简化取舍：直接按 tenant 前缀全删（同一 tenant 的路由缓存规模有限，
    通常数十条，全删成本低于逐条解析校对）。
    """

    tenant = (tenant_id or "").strip()
    if not tenant:
        return
    invalidate_namespace_pattern(NAMESPACE, f"{tenant}:*")
