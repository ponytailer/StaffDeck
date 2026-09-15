"""知识路由决策缓存（cache-aside，Redis 可选底座）。

缓存 LLM 路由（``knowledge.document_route`` / ``knowledge.bucket_route``）的
决策结果，让重复问法跳过 2~74s 的模型路由调用。

2026-09-14 重构要点：

1. **两维独立**：document 与 bucket 各有独立 key、独立写回条件。任一路走 LLM
   成功即可缓存该路决策；旧实现要求「两路都走 LLM 成功」才写，导致
   「doc 走词法快速路径 + bucket 走 LLM」这类组合一个字都不缓存。
2. **key 含版本维度**：``knowledge_base_version_ids`` 参与 key 指纹。换版本后
   候选集合整体变化，不会命中旧版本的决策。
3. **失效安全**：命中后按当前候选集过滤、按 ``max_*`` 截断；过滤为空视为缓存
   失效，回退 LLM。旧实现过滤为空直接返回「没有相关的知识」并静默持续到过期。
4. **精准失效**：写回时按 (tenant, kb_id) 登记索引集合，知识库写路径按库删除，
   不再整租户清空；TTL 因此可以放大（默认 12 小时），仅作兜底。
5. Redis 不可用 → 全部函数静默降级，行为与无缓存一致。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from app import object_cache

logger = logging.getLogger(__name__)

NAMESPACE = "kroute"
INDEX_NAMESPACE = "krouteidx"
_FULL_PREFIX = f"staffdeck:{NAMESPACE}:"

# 兜底 TTL（秒）。知识库写路径会精准失效，正常不会靠过期兜底，
# 所以给一个足够长的值以拉高命中率；漏失效时最长 12 小时后自动新鲜。
ROUTE_CACHE_TTL_SECONDS = 12 * 3600

# 负缓存（模型明确判定「没有相关候选」）的 TTL：短得多。这类结论可能只是
# 当前知识内容还没铺开，靠短 TTL + 知识库写路径失效双重兜底。
ROUTE_CACHE_NEGATIVE_TTL_SECONDS = 600

# 无显式知识库维度（agent 全局范围）时的索引 scope 名。
ALL_KB_SCOPE = "_all"

# 缓存维度：文档路由 / 内部索引路由
KIND_DOCUMENT = "doc"
KIND_BUCKET = "bucket"

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


@dataclass(frozen=True)
class RouteCacheSlot:
    """一条路由决策缓存位点：key + 失效索引所需的租户/知识库范围。"""

    key: str
    tenant_id: str
    kb_scopes: tuple[str, ...]

    @property
    def full_key(self) -> str:
        """Redis 中的完整键（含 ``staffdeck:kroute:`` 前缀）。"""

        return _FULL_PREFIX + self.key


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


def _unique_sorted(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _ttl_seconds() -> int:
    """缓存 TTL：优先读配置，异常/未配置时用模块默认值。"""

    try:
        from app.config import get_settings

        configured = int(getattr(get_settings(), "knowledge_route_cache_ttl_seconds", 0) or 0)
    except Exception:
        configured = 0
    return configured if configured > 0 else ROUTE_CACHE_TTL_SECONDS


def _negative_ttl_seconds() -> int:
    """负缓存 TTL：优先读配置，异常/未配置时用模块默认值。"""

    try:
        from app.config import get_settings

        configured = int(
            getattr(get_settings(), "knowledge_route_cache_negative_ttl_seconds", 0) or 0
        )
    except Exception:
        configured = 0
    return configured if configured > 0 else ROUTE_CACHE_NEGATIVE_TTL_SECONDS


def route_cache_slot(
    tenant_id: str,
    agent_id: str | None,
    knowledge_base_ids: list[str] | None,
    query: str,
    *,
    kind: str,
    knowledge_base_version_ids: list[str] | None = None,
) -> RouteCacheSlot | None:
    """构造缓存位点；无法构造（缺租户/查询归一后为空）时返回 None。"""

    tenant = (tenant_id or "").strip()
    normalized = query_norm(query or "")
    if not tenant or not normalized:
        return None
    agent = (agent_id or "").strip() or "-"
    kb_ids = _unique_sorted(knowledge_base_ids)
    version_ids = _unique_sorted(knowledge_base_version_ids)
    fingerprint = "|".join(
        (
            ",".join(kb_ids),
            ",".join(version_ids),
            normalized,
        )
    )
    digest = hashlib.md5(fingerprint.encode("utf-8")).hexdigest()[:12]
    key = f"{tenant}:{kind}:{agent}:{digest}"
    scopes = tuple(kb_ids) if kb_ids else (ALL_KB_SCOPE,)
    return RouteCacheSlot(key=key, tenant_id=tenant, kb_scopes=scopes)


def get_route_decision(slot: RouteCacheSlot | None) -> list[str] | None:
    """读该维度的路由决策 ID 列表；未命中/不可用/结构异常返回 None。"""

    if slot is None:
        return None
    data = object_cache.load_json(slot.key, namespace=NAMESPACE)
    if not isinstance(data, dict):
        return None
    ids = data.get("ids")
    if not isinstance(ids, list):
        return None
    return [str(item) for item in ids]


def store_route_decision(
    slot: RouteCacheSlot | None,
    ids: list[str] | None,
    *,
    allow_empty: bool = False,
) -> bool:
    """写该维度的路由决策；返回是否真正写入（Redis 不可用/空结果返回 False）。

    ``allow_empty=True`` 时把「模型明确判定为没有相关候选」也缓存下来（负缓存）。
    这类决策同样要花一次模型路由才能得到，不缓存等于每次白问一遍。TTL 取更短的
    ``ROUTE_CACHE_NEGATIVE_TTL_SECONDS``，因为「没有相关候选」可能只是当前内容
    还没铺开；知识库写路径的精准失效仍会主动清掉它。
    """

    if slot is None:
        return False
    if not ids and not allow_empty:
        return False
    ttl = _negative_ttl_seconds() if not ids else _ttl_seconds()
    written = object_cache.store_json(
        slot.key,
        {"ids": [str(item) for item in (ids or [])]},
        ttl_seconds=ttl,
        namespace=NAMESPACE,
    )
    if written:
        _register_index(slot, ttl)
    return written


def _index_key(tenant_id: str, scope: str) -> str:
    return f"staffdeck:{INDEX_NAMESPACE}:{tenant_id}:{scope}"


def _register_index(slot: RouteCacheSlot, ttl_seconds: int) -> None:
    """把该缓存键登记到所属的知识库索引集合，供按库精准失效。"""

    client = object_cache.get_redis()
    if client is None:
        return
    try:
        for scope in slot.kb_scopes:
            index_key = _index_key(slot.tenant_id, scope)
            client.sadd(index_key, slot.full_key)
            client.expire(index_key, ttl_seconds)
    except Exception:
        # 索引登记失败不影响缓存本身可用，最多退化为「该条不参与精准失效」
        logger.warning("路由缓存索引登记失败：%s", slot.key)


def invalidate_knowledge_base(
    tenant_id: str, knowledge_base_ids: list[str] | str | None = None
) -> None:
    """知识库内容变更时，删除该库相关的路由缓存。

    按写入时登记的索引集合删除，**不再整租户清空**：

    - 指定 kb：删除这些库的索引 + ``_all_``（agent 全局范围）索引；
    - 未指定：扫描并删除该租户的全部路由缓存索引（租户级全量失效）。

    索引集合用完即删；集合中残留的已删除键在 ``DEL`` 时是 no-op，不影响正确性。
    """

    tenant = (tenant_id or "").strip()
    if not tenant:
        return
    client = object_cache.get_redis()
    if client is None:
        return

    kb_ids = _unique_sorted(knowledge_base_ids)
    if kb_ids:
        scopes = sorted(set(kb_ids) | {ALL_KB_SCOPE})
        index_keys = [_index_key(tenant, scope) for scope in scopes]
    else:
        index_keys = list(
            client.scan_iter(match=f"staffdeck:{INDEX_NAMESPACE}:{tenant}:*", count=200)
        )

    for index_key in index_keys:
        try:
            members = client.smembers(index_key)
            batch = list(members)
            # 分批删除，避免单条命令参数过长阻塞 Redis
            for start in range(0, len(batch), 500):
                client.delete(*batch[start : start + 500])
            client.delete(index_key)
        except Exception:
            logger.warning("路由缓存失效失败：%s", index_key)
