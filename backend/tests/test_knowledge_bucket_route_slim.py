"""bucket_route payload 瘦身（2026-09-09）测试。

覆盖：
- _bucket_card_for_route 卡片构成：无 quality、section_paths ≤4×60、summary ≤160
- 卡片字符预算护栏（超长 summary 降级）
- SEARCH_BUCKET_ROUTE_LIMIT 候选裁剪（160+ 桶只送 60）
- 裁剪保留词法 ranked 优先语义
"""

from __future__ import annotations

import json

from app.db.models import KnowledgeBucket
from app.knowledge.service import (
    BUCKET_ROUTE_CARD_CHAR_BUDGET,
    SEARCH_BUCKET_ROUTE_LIMIT,
    _bucket_card_for_route,
    _route_candidates,
)


class _Bucket:
    """轻量桶桩（避免建 DB）：_bucket_card_for_route 只读这几个属性。"""

    def __init__(self, *, bucket_id: str, title: str, summary: str, metadata_json: dict) -> None:
        self.id = bucket_id
        self.document_id = "kdoc_x"
        self.title = title
        self.summary = summary
        self.metadata_json = metadata_json


def test_bucket_card_for_route_shape() -> None:
    bucket = _Bucket(
        bucket_id="kbucket_1",
        title="电脑申领流程",
        summary="员工申领电脑需要提交 OA 审批，审批通过后 IT 部门统一配发设备。",
        metadata_json={
            "bucket_type": "structure",
            "applicable_query_types": ["answer"],
            "section_paths": [
                {"path": f"第 {i} 章 申领流程"} for i in range(12)
            ],
            "quality": {"status": "ready", "warnings": [], "content_chars": 1234},
        },
    )
    card = _bucket_card_for_route(bucket)

    # 瘦身断言：quality 彻底移除；section_paths ≤4；字段受预算约束
    assert "quality" not in card
    assert len(card["section_paths"]) == 4
    assert len(card["summary"]) <= 160
    assert card["id"] == "kbucket_1"
    assert card["document_id"] == "kdoc_x"
    assert card["bucket_type"] == "structure"
    assert card["applicable_query_types"] == ["answer"]
    assert len(json.dumps(card, ensure_ascii=False)) <= BUCKET_ROUTE_CARD_CHAR_BUDGET


def test_bucket_card_for_route_char_budget_degrades() -> None:
    """summary 超长时卡片必须仍被压在预算内（逐级降级）。"""

    bucket = _Bucket(
        bucket_id="kbucket_long",
        title="长摘要桶",
        summary="内容" * 200,  # 400 chars 超长摘要
        metadata_json={"section_paths": ["路径" * 30, "路径" * 30, "路径" * 30, "路径" * 30, "x" * 55]},
    )
    card = _bucket_card_for_route(bucket)
    assert len(json.dumps(card, ensure_ascii=False)) <= BUCKET_ROUTE_CARD_CHAR_BUDGET


def test_bucket_card_for_route_empty_metadata() -> None:
    """metadata 缺失/空桶卡不炸、不产 None 字段噪声。"""

    bucket = _Bucket(bucket_id="kbucket_e", title="T", summary="S", metadata_json={})
    card = _bucket_card_for_route(bucket)
    assert card["section_paths"] == []
    assert card["applicable_query_types"] == []
    assert card["bucket_type"] is None


def test_bucket_route_candidate_limit() -> None:
    """160+ 候选桶只送 SEARCH_BUCKET_ROUTE_LIMIT(60) 个进 LLM。"""

    ranked = [_Bucket(bucket_id=f"kb_{i}", title=f"T{i}", summary="s", metadata_json={}) for i in range(200)]
    routed = _route_candidates(ranked, ranked, SEARCH_BUCKET_ROUTE_LIMIT)
    assert len(routed) == SEARCH_BUCKET_ROUTE_LIMIT
    # 词法 ranked 优先：前 60 全部来自 ranked 头部
    assert routed[0].id == "kb_0"
    assert routed[-1].id == f"kb_{SEARCH_BUCKET_ROUTE_LIMIT - 1}"


def test_bucket_route_candidates_fill_from_pool() -> None:
    """词法 ranked 只有 3 个命中时，补齐语义从全量池填满上限（不丢语义候选）。"""

    ranked = [_Bucket(bucket_id=f"kb_{i}", title=f"T{i}", summary="s", metadata_json={}) for i in range(3)]
    pool = ranked + [
        _Bucket(bucket_id=f"pool_{i}", title=f"P{i}", summary="s", metadata_json={})
        for i in range(100)
    ]
    routed = _route_candidates(ranked, pool, SEARCH_BUCKET_ROUTE_LIMIT)
    assert len(routed) == SEARCH_BUCKET_ROUTE_LIMIT
    assert {b.id for b in routed[:3]} == {"kb_0", "kb_1", "kb_2"}
    # 补齐部分来自 pool 未入选部分
    assert any(b.id.startswith("pool_") for b in routed[3:])
