"""知识检索耗时归因探针（只读业务数据，会写 kroute 缓存键）。

回答两个问题：

1. **同一 query 连问两次，第二次到底有没有命中路由缓存？** —— 打印两次构造的
   doc / bucket 缓存位点 key 与命中情况。
2. **knowledge.search 的耗时花在哪？** —— 用 span sink 捕获所有
   ``knowledge_span_finished`` 事件，逐阶段打印 duration_ms。

用法：
    cd backend && .venv/bin/python scripts/probe_knowledge_search_latency.py [kb_id] [tenant_id] [model_config_id]
    # 自定义 query（可多次传 --query）：
    cd backend && .venv/bin/python scripts/probe_knowledge_search_latency.py --query "项目上是否设有医务室"

注意：走 LLM 路由时会触发真实模型调用。
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db.database import engine, get_session  # noqa: E402
from app.knowledge.route_cache import (  # noqa: E402
    KIND_BUCKET,
    KIND_DOCUMENT,
    get_route_decision,
    query_norm,
    route_cache_slot,
)
from app.knowledge.schema import KnowledgeSearchRequest  # noqa: E402
from app.knowledge.service import KnowledgeService  # noqa: E402
from app.llm.model_config_resolver import resolve_model_config_for_runtime  # noqa: E402
from app.observability.spans import bind_span_sink  # noqa: E402

DEFAULT_KB = "kb_preset_project_001"
DEFAULT_TENANT = "tenant_demo"
DEFAULT_CONFIG = "model_746183803f604524"  # glm-5.3-flash

# 复现「同一个问题问两遍仍很慢」：用户原话相同，但模型每轮改写的 query 不同。
# 括号内为 (改写后的 query, 缓存用的用户原话)。
DEFAULT_RUNS = [
    ("项目上是否设有医务室", "有医务室吗"),
    ("项目 医务室 位置 使用方式", "有医务室吗"),
]


def kb_stats(kb_id: str) -> None:
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


def slot_keys(
    kb_id: str,
    tenant_id: str,
    query: str,
    version_ids: list[str] | None,
    cache_query: str | None = None,
) -> None:
    """打印本次会用到的缓存位点与命中内容（稳定位点优先，query 位点兜底）。"""

    printed: set[str] = set()
    for kind, label in ((KIND_DOCUMENT, "doc"), (KIND_BUCKET, "bucket")):
        for tag, text in (("stable", cache_query), ("query", query)):
            if not str(text or "").strip():
                continue
            slot = route_cache_slot(
                tenant_id, None, [kb_id], text, kind=kind,
                knowledge_base_version_ids=version_ids,
            )
            if slot is None or slot.key in printed:
                continue
            printed.add(slot.key)
            hit = get_route_decision(slot)
            print(
                f"    {label:<6} {tag:<6} key={slot.key}"
                f"  cached_ids={'None(未命中)' if hit is None else hit}"
            )


def run_one(
    service: KnowledgeService,
    tenant_id: str,
    kb_id: str,
    query: str,
    cache_query: str,
    model_config: Any,
    spans: list[tuple[str, dict[str, Any]]],
) -> None:
    request = KnowledgeSearchRequest(
        tenant_id=tenant_id,
        agent_id=None,
        query=query,
        cache_query=cache_query,
        knowledge_base_ids=[kb_id],
        mode="debug",
        need_evidence_pack=True,
    )
    print(f"  query={query!r}  cache_query={cache_query!r}  query_norm={query_norm(query)!r}")
    slot_keys(kb_id, tenant_id, query, request.knowledge_base_version_ids, cache_query)

    spans.clear()
    started = time.perf_counter()
    resp = service.search(request, model_config)
    elapsed = time.perf_counter() - started

    print(f"  ── 总耗时 {elapsed:.2f}s")
    print("  ── 阶段耗时（knowledge_span，按 duration 降序）")
    rows = [
        (payload.get("duration_ms") or 0.0, payload.get("operation") or "?", payload)
        for _ev, payload in spans
    ]
    rows.sort(reverse=True, key=lambda item: item[0])
    for dur, operation, payload in rows:
        extra = ""
        if payload.get("strategy"):
            extra += f" strategy={payload['strategy']}"
        if payload.get("candidate_count") is not None:
            extra += f" candidates={payload['candidate_count']}"
        if payload.get("selected_count") is not None:
            extra += f" selected={payload['selected_count']}"
        print(f"    {dur:>9.1f}ms  {operation}{extra}")
    phases = [str(item.get("phase") or "?") for item in (resp.route_trace or [])]
    print(f"  ── phases = {phases}")
    print(
        f"  ── 选中文档 {len(resp.selected_documents or [])} 篇 / "
        f"内部索引 {len(resp.selected_buckets or [])} 个 / "
        f"chunk {len(resp.chunks or [])} 段"
    )


def main() -> None:
    argv = [item for item in sys.argv[1:]]
    queries: list[str] = []
    cache_query_arg: str | None = None
    positional: list[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == "--query" and index + 1 < len(argv):
            queries.append(argv[index + 1])
            index += 2
            continue
        if argv[index] == "--cache-query" and index + 1 < len(argv):
            cache_query_arg = argv[index + 1]
            index += 2
            continue
        positional.append(argv[index])
        index += 1

    kb_id = positional[0] if len(positional) > 0 else DEFAULT_KB
    tenant_id = positional[1] if len(positional) > 1 else DEFAULT_TENANT
    config_id = positional[2] if len(positional) > 2 else DEFAULT_CONFIG
    runs = (
        [(query, cache_query_arg or query) for query in queries]
        if queries
        else list(DEFAULT_RUNS)
    )

    kb_stats(kb_id)
    print()

    session = next(get_session())
    spans: list[tuple[str, dict[str, Any]]] = []

    def collect(event_type: str, payload: dict[str, Any]) -> None:
        # 只收「已结束」的 span（带 duration_ms），started 事件无耗时信息
        if payload.get("duration_ms") is not None:
            spans.append((event_type, payload))

    try:
        model_config = resolve_model_config_for_runtime(session, tenant_id, config_id)
        service = KnowledgeService(session)
        for seq, (query, cache_query) in enumerate(runs, 1):
            print(f"\n{'=' * 72}\n[第 {seq} 次]")
            with bind_span_sink(collect):
                run_one(service, tenant_id, kb_id, query, cache_query, model_config, spans)
    finally:
        session.close()


if __name__ == "__main__":
    main()
