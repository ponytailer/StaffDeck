"""回归：疑问句里的噪声外壳不能变成「检索关键词」。

2026-10-23 线上问题（重庆 AI G.O / kb_9fa98ee6b2ce4c39）：
- 「怎么去啊」→ 旧 ``_query_terms`` 全局 replace 掉「怎么」得到残片「去啊」，
  库里不存在该词 → 51 个 chunk 词法全部 0 分 → 检索空结果；
- 「怎么去项目」→ 同样剥出「去项目」，n-gram 命中泛词「项目」→ 51 块里
  23 块通过门槛，全部是与「怎么去」无关的项目介绍（假命中）。

同一意图两种问法走向完全不同的结果，根因是「在词中间挖洞造词」+「泛词
当实体」。本文件锁定修复后的不变量：
1. 噪声外壳只在词项首尾剥离，绝不粘出新词（不产生「去啊」「去重」）；
2. 纯虚字残片与高频泛词不参与词法召回；
3. 两个问法词法上都是「零信号」，落到同一条兜底路径（各桶代表块）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import KnowledgeBase, KnowledgeBucket, KnowledgeChunk, KnowledgeDocument, Tenant
from app.knowledge import service as knowledge_service
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import (
    KnowledgeService,
    _query_terms,
    _rank_chunks,
    _weak_fallback_chunks,
)


def _test_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


# ---------------------------------------------------------------------------
# 词项抽取：外壳剥离不得造词
# ---------------------------------------------------------------------------


def test_noise_stripping_never_glues_neighbouring_chars() -> None:
    """「怎么去啊」不得产出「去啊」，「怎么去重庆」不得产出「去重」。"""

    assert _query_terms("怎么去啊") == []
    assert "去啊" not in _query_terms("怎么去啊")
    cn_terms = _query_terms("怎么去重庆")
    assert "重庆" in cn_terms
    assert "去重" not in cn_terms


def test_intent_pair_yields_identical_empty_terms() -> None:
    """同一意图的两种问法必须同解——这是用户报的核心症状。"""

    assert _query_terms("怎么去啊") == _query_terms("怎么去项目") == []


def test_entity_terms_survive_noise_stripping() -> None:
    """实体词不能被「泛词/虚字」过滤误杀。"""

    terms = _query_terms("重庆AIGO怎么去")
    assert "重庆" in terms
    assert "aigo" in terms

    fire = _query_terms("消防设备怎么配")
    assert "消防" in fire and "设备" in fire

    decoration = _query_terms("装修要求有哪些")
    assert "装修" in decoration


def test_generic_nouns_alone_do_not_recall() -> None:
    """单靠泛词（「项目」「内容」）不得构成召回依据。"""

    assert _query_terms("项目") == []
    assert _query_terms("内容有哪些") == []


# ---------------------------------------------------------------------------
# 端到端：零词法信号落到同一条兜底路径
# ---------------------------------------------------------------------------


def _seed_project_kb(db: Session) -> None:
    """模拟「百问百搭」单文档资料：块块都写「项目」，但没有交通指引。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="项目百问"))
    document = KnowledgeDocument(
        id="kdoc_project",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_demo",
        filename="project.md",
        file_type="md",
        title="Hi Sphere 重庆项目百问百搭",
        status="ready",
        bucket_count=2,
        chunk_count=4,
    )
    db.add(document)
    overview = KnowledgeBucket(
        id="kbucket_overview",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_demo",
        document_id=document.id,
        bucket_key="overview",
        title="项目基本信息",
        summary="项目定位、区位与开业时间。",
    )
    facility = KnowledgeBucket(
        id="kbucket_facility",
        tenant_id="tenant_demo",
        knowledge_base_id="kb_demo",
        document_id=document.id,
        bucket_key="facility",
        title="设备参数",
        summary="乐园设备高度、速度与承载人数。",
    )
    db.add(overview)
    db.add(facility)
    rows = [
        (overview, 0, "项目位于重庆两江新区，临近江北国际机场，计划 12 月 18 日开业。"),
        (overview, 1, "项目由两江新区保税港承建，总建筑面积约 30 万平方米。"),
        (facility, 0, "九霄云巅总高度 48 米，单次最大承载 20 人。"),
        (facility, 1, "雷霆战车最高时速 96 公里，轨道最高 43.5 米。"),
    ]
    for index, (bucket, chunk_index, text) in enumerate(rows):
        db.add(
            KnowledgeChunk(
                id=f"kchunk_{index}",
                tenant_id="tenant_demo",
                knowledge_base_id="kb_demo",
                document_id=document.id,
                bucket_id=bucket.id,
                chunk_index=chunk_index,
                content=text,
                summary=text,
                source_ref="project.md",
            )
        )
    db.commit()


@pytest.fixture()
def _no_route_cache(monkeypatch):
    """路由缓存位点置空：单测不依赖 Redis。"""

    monkeypatch.setattr(knowledge_service, "route_cache_slot", lambda *args, **kwargs: None)


def _search(db: Session, query: str):
    return KnowledgeService(db).search(
        KnowledgeSearchRequest(
            tenant_id="tenant_demo",
            knowledge_base_ids=["kb_demo"],
            query=query,
            mode="chat",
            need_evidence_pack=False,
        ),
        model_config=object(),  # 非 None 即走「有模型」分支；小候选短路下不会真调模型
    )


def test_both_phrasings_return_same_representative_chunks(_no_route_cache) -> None:
    with _test_session() as db:
        _seed_project_kb(db)
        vague = _search(db, "怎么去啊")
        explicit = _search(db, "怎么去项目")

        vague_phases = [item["phase"] for item in vague.route_trace]
        explicit_phases = [item["phase"] for item in explicit.route_trace]
        assert "chunk_lexical_empty_fallback" in vague_phases
        assert "chunk_lexical_empty_fallback" in explicit_phases

        vague_ids = [chunk.id for chunk in vague.chunks]
        explicit_ids = [chunk.id for chunk in explicit.chunks]
        assert vague_ids
        assert vague_ids == explicit_ids


def test_weak_fallback_takes_first_chunk_per_bucket(_no_route_cache) -> None:
    with _test_session() as db:
        _seed_project_kb(db)
        response = _search(db, "怎么去啊")
        # 每桶首块：两桶各一块，不含桶内第二块
        assert {chunk.id for chunk in response.chunks} == {"kchunk_0", "kchunk_2"}


def test_normal_query_does_not_trigger_fallback(_no_route_cache) -> None:
    with _test_session() as db:
        _seed_project_kb(db)
        response = _search(db, "雷霆战车")
        phases = [item["phase"] for item in response.route_trace]
        assert "chunk_lexical_empty_fallback" not in phases
        assert [chunk.id for chunk in response.chunks] == ["kchunk_3"]


# ---------------------------------------------------------------------------
# 底层函数：兜底只取代表块，且不越过上限
# ---------------------------------------------------------------------------


def test_weak_fallback_respects_bucket_limit(monkeypatch) -> None:
    monkeypatch.setattr(knowledge_service, "WEAK_FALLBACK_MAX_BUCKETS", 2)
    chunks = [
        KnowledgeChunk(
            id=f"chunk_{bucket}_{index}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id="doc_demo",
            bucket_id=f"bucket_{bucket}",
            chunk_index=index,
            content="内容",
            summary="内容",
        )
        for bucket in range(4)
        for index in range(2)
    ]
    buckets = [
        KnowledgeBucket(
            id=f"bucket_{bucket}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id="doc_demo",
            bucket_key=f"b{bucket}",
            title=f"桶 {bucket}",
            summary="",
        )
        for bucket in range(4)
    ]
    picked = _weak_fallback_chunks(chunks, buckets)
    assert [chunk.id for chunk in picked] == ["chunk_0_0", "chunk_1_0"]


def test_rank_chunks_returns_empty_for_zero_signal_query() -> None:
    chunks = [
        KnowledgeChunk(
            id="chunk_only",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id="doc_demo",
            bucket_id="bucket_demo",
            chunk_index=0,
            content="项目位于重庆两江新区。",
            summary="项目位于重庆两江新区。",
        )
    ]
    assert _rank_chunks("怎么去啊", chunks, [], []) == []
