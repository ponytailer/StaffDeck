"""知识路由决策缓存（route_cache）测试。

覆盖：
- query_norm 归一聚合（相似问法同 key）
- route_cache_key 构造边界
- 缓存命中：跳过 LLM 路由，route_trace 有 route_cache_hit
- 缓存未命中：LLM 决策成功后写回，route_trace 有 route_cache_stored
- 词法快速路径结果不回写缓存
- LLM 失败词法兜底结果不回写缓存
- 失效调用后缓存清空（下次重新走 LLM）
- Redis 不可用（get_redis 返回 None）时全链路与无缓存一致
"""

from __future__ import annotations

import fnmatch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db.models import (
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    ModelConfig,
    Tenant,
)
from app.knowledge.route_cache import (
    invalidate_knowledge_base,
    query_norm,
    route_cache_key,
    store_route_cache,
)
from app.knowledge.schema import KnowledgeSearchRequest
from app.knowledge.service import KnowledgeService
from app.object_cache import load_json, store_json

MODEL_ROUTE = None  # placeholder，测试内构造


class _FakeRedis:
    """内存版 redis 客户端（decode_responses 语义：key/value 均为 str）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str):
        return self.data.get(key)

    def set(self, key: str, value: str, ex=None):  # noqa: ARG002
        self.data[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)

    def scan_iter(self, match: str | None = None, count: int | None = None):  # noqa: ARG002
        return [key for key in list(self.data) if fnmatch.fnmatchcase(key, match)]


@pytest.fixture()
def fake_redis(monkeypatch):
    """把 object_cache 的 Redis 底座替换为内存实现。"""

    import app.object_cache as object_cache

    client = _FakeRedis()
    monkeypatch.setattr(object_cache, "get_redis", lambda: client)
    return client


def _test_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _seed_ambiguous_fixture(db) -> None:
    """两个词法同分的文档+桶+chunk，强制走 LLM 路由分支。"""

    db.add(Tenant(id="tenant_demo", name="Demo"))
    db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
    for index, (title, summary) in enumerate(
        [
            ("员工手册", "入职流程与考勤规则说明"),
            ("员工手册附录", "考勤与假期规则补充"),
        ]
    ):
        document = KnowledgeDocument(
            id=f"kdoc_{index}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            filename=f"doc{index}.md",
            file_type="md",
            title=title,
            status="ready",
            metadata_json={"document_card": {"title": title, "summary": summary}},
        )
        bucket = KnowledgeBucket(
            id=f"kbucket_{index}",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key=f"section_{index}",
            title=title,
            summary=summary,
        )
        chunk = KnowledgeChunk(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content=summary,
        )
        db.add(document)
        db.add(bucket)
        db.add(chunk)
    db.commit()


def _route_model() -> ModelConfig:
    return ModelConfig(id="model_route", tenant_id="tenant_demo", name="Route", model="route")


# ---------------------------------------------------------------------------
# 纯函数：query_norm / route_cache_key
# ---------------------------------------------------------------------------


def test_query_norm_aggregates_similar_phoarings() -> None:
    assert query_norm("如何申领电脑") == query_norm("怎么申领电脑")
    assert query_norm("如何申领电脑") == query_norm("我想知道如何申领电脑？")
    assert query_norm("如何申领电脑") == query_norm("申领电脑")
    assert query_norm("如何申领电脑") == query_norm(" 请问 如何 申领 电脑 ？")
    # 空白/标点/大小写
    assert query_norm("Hello World！") == "helloworld"


def test_route_cache_key_boundaries() -> None:
    # 空租户 / 空 query / 纯噪声 query → None
    assert route_cache_key("", None, ["kb"], "查询") is None
    assert route_cache_key("tenant", None, ["kb"], "") is None
    assert route_cache_key("tenant", None, ["kb"], "？？？") is None
    # 正常构造
    key = route_cache_key("tenant_demo", "agent_a", ["kb_b", "kb_a"], "如何申领电脑")
    assert key == f"tenant_demo:agent_a:{route_cache_key('tenant_demo', 'agent_a', ['kb_a', 'kb_b'], '怎么申领电脑').split(':', 2)[2]}"
    # agent 缺省 → "-"
    assert route_cache_key("tenant_demo", None, ["kb"], "查询").split(":")[1] == "-"
    # kb 顺序无关
    assert route_cache_key("t", None, ["kb1", "kb2"], "查询") == route_cache_key(
        "t", None, ["kb2", "kb1"], "查询"
    )


# ---------------------------------------------------------------------------
# 集成：命中 / 写回 / 不回写 / 失效 / Redis 不可用
# ---------------------------------------------------------------------------


def test_route_cache_hit_skips_llm_routing(fake_redis, monkeypatch) -> None:
    """命中缓存：直接用缓存 ID，LLM 路由完全不被调用。"""

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on route cache hit")

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        key = route_cache_key("tenant_demo", None, ["kb_demo"], "员工手册")
        assert key
        store_route_cache(key, ["kdoc_1"], ["kbucket_1"])
        monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
        monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="员工手册",
                mode="chat",
            ),
            _route_model(),
        )

    phases = [item.get("phase") for item in response.route_trace]
    assert "route_cache_hit" in phases
    assert "route_cache_stored" not in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_1"]


def test_route_cache_stored_after_llm_decision(fake_redis, monkeypatch) -> None:
    """未命中 → LLM 决策成功 → 写回缓存；相同问法第二次搜索命中。"""

    def fake_select_documents(*args, **kwargs):  # noqa: ANN002, ANN003
        return ["kdoc_0", "kdoc_1"]

    def fake_select_buckets(*args, **kwargs):  # noqa: ANN002, ANN003
        return ["kbucket_0"]

    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", fake_select_documents
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", fake_select_buckets
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="员工手册",
                mode="chat",
            ),
            _route_model(),
        )
        phases = [item.get("phase") for item in response.route_trace]
        assert "route_cache_stored" in phases

        key = route_cache_key("tenant_demo", None, ["kb_demo"], "员工手册")
        cached = load_json(key, namespace="kroute")
        assert cached == {"document_ids": ["kdoc_0", "kdoc_1"], "bucket_ids": ["kbucket_0"]}

    # 第二次：噪声词变体同 key，且 LLM 不再被调用
    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on route cache hit")

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="我想知道员工手册？",
                mode="chat",
            ),
            _route_model(),
        )
    phases = [item.get("phase") for item in response.route_trace]
    assert "route_cache_hit" in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]


def test_route_cache_not_written_on_lexical_fast_path(fake_redis, monkeypatch) -> None:
    """词法快速路径的选择不回写缓存（避免固化非 LLM 决策）。"""

    def _forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("LLM routing must not be called on decisive lexical match")

    with _test_session() as db:
        # 复用双主题 fixture：前端规范 vs 离职办理，词法显著命中走快速路径
        db.add(Tenant(id="tenant_demo", name="Demo"))
        db.add(KnowledgeBase(id="kb_demo", tenant_id="tenant_demo", name="默认知识库"))
        document = KnowledgeDocument(
            id="kdoc_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            filename="frontend.md",
            file_type="md",
            title="前端编码规范",
            status="ready",
            metadata_json={
                "document_card": {"title": "前端编码规范", "summary": "前端编码规范与组件命名规范。"}
            },
        )
        bucket = KnowledgeBucket(
            id="kbucket_frontend",
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_key="frontend",
            title="前端编码规范",
            summary="Vue 3、Vite、TypeScript、组件编写和命名规范。",
        )
        chunk = KnowledgeChunk(
            tenant_id="tenant_demo",
            knowledge_base_id="kb_demo",
            document_id=document.id,
            bucket_id=bucket.id,
            chunk_index=0,
            content="前端编码规范包括 Vue 3、Vite、TypeScript 和组件命名规范。",
        )
        db.add_all([document, bucket, chunk])
        db.commit()
        monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", _forbidden)
        monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", _forbidden)

        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="前端编码规范",
                mode="chat",
            ),
            _route_model(),
        )

    phases = [item.get("phase") for item in response.route_trace]
    assert "document_route_lexical_fast_path" in phases
    assert "bucket_route_lexical_fast_path" in phases
    assert "route_cache_stored" not in phases
    # 缓存确实是空的
    key = route_cache_key("tenant_demo", None, ["kb_demo"], "前端编码规范")
    assert load_json(key, namespace="kroute") is None


def test_route_cache_not_written_on_llm_failure_fallback(fake_redis, monkeypatch) -> None:
    """LLM 失败走词法兜底时，兜底选择不回写缓存。"""

    monkeypatch.setattr(KnowledgeService, "_select_documents_with_llm", lambda *a, **k: None)
    monkeypatch.setattr(KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: None)

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="员工手册",
                mode="chat",
            ),
            _route_model(),
        )
    phases = [item.get("phase") for item in response.route_trace]
    assert "document_route_lexical_fallback" in phases
    assert "route_cache_stored" not in phases
    key = route_cache_key("tenant_demo", None, ["kb_demo"], "员工手册")
    assert load_json(key, namespace="kroute") is None


def test_route_cache_invalidation_clears_entries(fake_redis) -> None:
    """失效调用后，该 tenant 的全部路由缓存被清空。"""

    key_a = route_cache_key("tenant_demo", None, ["kb_demo"], "如何申领电脑")
    key_b = route_cache_key("tenant_demo", "agent_x", ["kb_demo"], "怎么申领电脑")
    key_other = route_cache_key("tenant_other", None, ["kb_demo"], "如何申领电脑")
    store_route_cache(key_a, ["d"], ["b"])
    store_route_cache(key_b, ["d"], ["b"])
    store_route_cache(key_other, ["d"], ["b"])

    invalidate_knowledge_base("tenant_demo", "kb_demo")

    assert load_json(key_a, namespace="kroute") is None
    assert load_json(key_b, namespace="kroute") is None
    # 其他租户不受影响
    assert load_json(key_other, namespace="kroute") is not None


def test_route_cache_search_works_without_redis(monkeypatch) -> None:
    """Redis 不可用：读 None、写静默跳过，行为与无缓存完全一致。"""

    import app.object_cache as object_cache

    monkeypatch.setattr(object_cache, "get_redis", lambda: None)
    monkeypatch.setattr(
        KnowledgeService, "_select_documents_with_llm", lambda *a, **k: ["kdoc_0"]
    )
    monkeypatch.setattr(
        KnowledgeService, "_select_buckets_with_llm", lambda *a, **k: ["kbucket_0"]
    )

    with _test_session() as db:
        _seed_ambiguous_fixture(db)
        response = KnowledgeService(db).search(
            KnowledgeSearchRequest(
                tenant_id="tenant_demo",
                knowledge_base_ids=["kb_demo"],
                query="员工手册",
                mode="chat",
            ),
            _route_model(),
        )
    phases = [item.get("phase") for item in response.route_trace]
    assert "route_cache_hit" not in phases
    assert "route_cache_stored" not in phases
    assert [bucket.id for bucket in response.selected_buckets] == ["kbucket_0"]
