"""开放广场首页新闻接口（plaza_news）测试。

不真发 HTTP：mock `app.api.plaza_news._fetch_news` 验证缓存与回退链。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.api import plaza_news as pn
from app.db.models import PlazaNewsCache, Tenant

RSS_OK = {
    "ai": [{"category": "ai", "title": "AI Title", "summary": "s", "source": "x.com", "url": "https://x.com/a", "published_at": ""}],
    "travel": [{"category": "travel", "title": "Travel Title", "summary": "s", "source": "t.com", "url": "https://t.com/b", "published_at": ""}],
}

TENANT = "tenant_plaza_news"


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plaza-news.db'}")
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=TENANT, name="Tenant"))
    session.commit()
    yield session
    session.close()


def test_same_day_cache_hit(db, monkeypatch):
    """当天第二次调用不再抓取，直接返回缓存。"""
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return RSS_OK

    monkeypatch.setattr(pn, "_fetch_news", fake_fetch)
    first = pn.get_plaza_news_impl(db, TENANT)
    second = pn.get_plaza_news_impl(db, TENANT)
    assert calls["n"] == 1
    assert first["date"] == second["date"]
    assert first["provenance"] == "rss"
    assert second["items"]["ai"][0]["title"] == "AI Title"


def test_fetch_failure_falls_back_to_last_cache(db, monkeypatch):
    """今天抓取失败 → 回退最近一天缓存，缺失类目用种子补齐。"""
    yesterday = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)).strftime("%Y-%m-%d")
    db.add(
        PlazaNewsCache(
            tenant_id=TENANT,
            cache_date=yesterday,
            items_json={"ai": RSS_OK["ai"], "travel": []},  # 旅文缺
            provenance="rss",
        )
    )
    db.commit()

    def fake_fetch():
        return {"ai": [], "travel": []}  # 全失败

    monkeypatch.setattr(pn, "_fetch_news", fake_fetch)
    payload = pn.get_plaza_news_impl(db, TENANT)
    assert payload["date"] == yesterday
    assert payload["items"]["travel"], "缺失类目应被种子补齐"


def test_total_failure_seeds_fallback_and_caches(db, monkeypatch):
    """无任何历史缓存 + 抓取失败 → 种子兜底，且落当天缓存（provenance=fallback）。"""
    def fake_fetch():
        return {"ai": [], "travel": []}

    monkeypatch.setattr(pn, "_fetch_news", fake_fetch)
    payload = pn.get_plaza_news_impl(db, TENANT)
    assert payload["provenance"] == "fallback"
    assert payload["items"]["ai"] and payload["items"]["travel"]

    # 当天缓存已落；再调直接命中，不重复构造
    rows = db.exec(select(PlazaNewsCache).where(PlazaNewsCache.tenant_id == TENANT)).all()
    assert len(rows) == 1
    assert rows[0].cache_date == payload["date"]


def test_clean_text_strips_html():
    assert pn._clean_text("<p>你好 <b>世界</b></p>", limit=50) == "你好 世界"
    assert pn._clean_text(None) == ""
    cleaned = pn._clean_text("字" * 200, limit=10)
    assert len(cleaned) == 11  # 10 字 + 省略号
    assert cleaned.endswith("…")


def test_parse_feed_rss_and_atom():
    rss = """<?xml version="1.0"?>
    <rss><channel>
      <item><title>标题一</title><link>https://a.com/1</link><description>&lt;p&gt;摘要&lt;/p&gt;</description></item>
      <item><title>无链接条目</title><description>应被跳过</description></item>
    </channel></rss>"""
    items = pn._parse_feed(rss, "ai")
    assert len(items) == 1
    assert items[0]["title"] == "标题一"
    assert items[0]["url"] == "https://a.com/1"
    assert items[0]["summary"] == "摘要"
    assert items[0]["source"] == "a.com"

    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Atom Entry</title><link href="https://b.com/2"/><summary>sum</summary></entry>
    </feed>"""
    items = pn._parse_feed(atom, "travel")
    assert len(items) == 1
    assert items[0]["url"] == "https://b.com/2"
    assert items[0]["category"] == "travel"
