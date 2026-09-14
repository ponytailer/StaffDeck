"""知识路由缓存端到端探针（只读业务数据，仅读写 kroute 缓存键）。

用途：回答「知识路由缓存是否生效 / 命中率如何」。用同一意图的多种问法跑真实
search，观察 route_trace 里的 phase 与耗时差，并核对两维（doc / bucket）是否
独立写回与独立命中。

用法：
    cd backend && .venv/bin/python scripts/probe_knowledge_route_cache.py
    cd backend && .venv/bin/python scripts/probe_knowledge_route_cache.py <kb_id> [tenant_id]

注意：会触发真实 LLM 路由调用（首次/未命中时），耗时可达数十秒。
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db.database import engine, get_session  # noqa: E402
from app.knowledge.route_cache import (  # noqa: E402
    KIND_BUCKET,
    KIND_DOCUMENT,
    query_norm,
    route_cache_slot,
)
from app.knowledge.schema import KnowledgeSearchRequest  # noqa: E402
from app.knowledge.service import KnowledgeService  # noqa: E402
from app.llm.model_config_resolver import resolve_model_config_for_runtime  # noqa: E402
from app.redis_client import get_redis  # noqa: E402

DEFAULT_KB = "kb_894511261b6d402f"  # 财务-报销政策手册
DEFAULT_TENANT = "tenant_demo"
DEFAULT_CONFIG = "model_b87b39d2c5774ccc"  # deepseek-flash / deepseek-v4-flash

# 同一意图的多种问法：期望 1/2 归一后同 key，3/4 为同义改写（不同 key）
QUERIES = [
    ("原句    ", "差旅费报销标准是什么"),
    ("加噪声词", "请问一下，差旅费报销标准是什么？"),
    ("同义改写", "出差费用能报销多少"),
]


def kb_stats(tenant_id: str, kb_id: str) -> None:
    with engine.connect() as conn:
        docs = conn.execute(
            text("select count(*) from knowledge_documents where knowledge_base_id = :kb"),
            {"kb": kb_id},
        ).scalar()
        buckets = conn.execute(
            text(
                "select count(*) from knowledge_buckets b join knowledge_documents d "
                "on b.document_id = d.id where d.knowledge_base_id = :kb"
            ),
            {"kb": kb_id},
        ).scalar()
    print(f"知识库 {kb_id}：文档 {docs} 篇 / 内部索引 {buckets} 个")


def phases_of(resp) -> list[str]:
    return [str(item.get("phase") or "?") for item in (resp.route_trace or [])]


def dump_cache(label: str) -> None:
    client = get_redis()
    if client is None:
        print(f"\n[{label}] Redis 不可用 → 缓存必然不生效")
        return
    keys = [
        k.decode() if isinstance(k, bytes) else k
        for k in client.scan_iter(match="staffdeck:kroute:*", count=500)
    ]
    idx = [
        k.decode() if isinstance(k, bytes) else k
        for k in client.scan_iter(match="staffdeck:krouteidx:*", count=500)
    ]
    print(f"\n[{label}] kroute 键 {len(keys)} 个 / krouteidx 索引 {len(idx)} 个")
    for k in keys:
        print("   ", k, "ttl =", client.ttl(k))
    for k in idx:
        members = client.smembers(k)
        print("   ", k, "members =", len(members), "ttl =", client.ttl(k))


def main() -> None:
    kb_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_KB
    tenant_id = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TENANT

    kb_stats(tenant_id, kb_id)
    print()

    for label, query in QUERIES:
        doc_slot = route_cache_slot(
            tenant_id, None, [kb_id], query, kind=KIND_DOCUMENT
        )
        bucket_slot = route_cache_slot(
            tenant_id, None, [kb_id], query, kind=KIND_BUCKET
        )
        print(f"[{label}] query={query!r}  query_norm={query_norm(query)!r}")
        print(f"            doc_slot    = {doc_slot.key if doc_slot else None}")
        print(f"            bucket_slot = {bucket_slot.key if bucket_slot else None}")

    print()
    print("=" * 72)

    session = next(get_session())
    try:
        model_config = resolve_model_config_for_runtime(session, tenant_id, DEFAULT_CONFIG)
        service = KnowledgeService(session)

        # 先各跑一次（首次），再各重放一次（验证命中）
        runs = [
            ("原句-1st", QUERIES[0][1]),
            ("同义-1st", QUERIES[2][1]),
            ("原句-2nd(重放)", QUERIES[0][1]),
            ("同义-2nd(重放)", QUERIES[2][1]),
        ]
        for label, query in runs:
            request = KnowledgeSearchRequest(
                tenant_id=tenant_id,
                agent_id=None,
                query=query,
                knowledge_base_ids=[kb_id],
                mode="debug",
            )
            started = time.perf_counter()
            try:
                resp = service.search(request, model_config)
                elapsed = time.perf_counter() - started
                print(f"\n[{label}] {query!r}  耗时 {elapsed:.1f}s")
                print(f"  phases = {phases_of(resp)}")
                print(
                    f"  选中文档 {len(resp.selected_documents or [])} 篇 / "
                    f"内部索引 {len(resp.selected_buckets or [])} 个 / "
                    f"chunk {len(resp.chunks or [])} 段"
                )
            except Exception as exc:  # noqa: BLE001 - 探针需打印失败原因
                elapsed = time.perf_counter() - started
                print(f"\n[{label}] {query!r}  失败（{elapsed:.1f}s）：{type(exc).__name__}: {exc}")
        dump_cache("执行后")
    finally:
        session.close()


if __name__ == "__main__":
    main()
