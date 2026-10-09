"""开放广场首页新闻（AI 圈 / 旅文圈）。

设计口径（用户约定「一日一更新」）：

- ``GET /api/enterprise/plaza-news`` 先查当天（按天粒度）缓存，命中直接返回；
- 未命中则抓一次 RSS 源（AI 圈 + 旅文圈各若干源），清洗后落 ``plaza_news_cache``；
- 抓取失败（外网不通/超时/解析失败）回退最近一次缓存，连缓存都没有时退到
  内置种子新闻（``provenance="fallback"``），保证接口永远有内容可渲染。

RSS 解析用标准库 ``xml.etree``（RSS 2.0 + Atom 均覆盖），HTML 清洗用已依赖的
bs4，不新增第三方包。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db.database import get_session
from app.db.models import PlazaNewsCache
from app.security.auth import get_current_user, require_current_tenant

router = APIRouter(
    prefix="/api/enterprise/plaza-news",
    tags=["enterprise:plaza-news"],
    dependencies=[Depends(get_current_user)],
)

# 抓取配置：每个类目按顺序尝试，凑够 MAX_PER_CATEGORY 条即停。
# 源不可达/格式变化都只是「跳过该源」，不影响其余源。
# 口径（用户约定）：全部使用国内新闻源。
FEEDS: dict[str, list[str]] = {
    "ai": [
        # 量子位（WordPress RSS）
        "https://www.qbitai.com/feed",
        # 机器之心
        "https://www.jiqizhixin.com/rss",
        # 爱范儿（备用，WordPress RSS）
        "https://www.ifanr.com/feed",
    ],
    "travel": [
        # 环球旅讯
        "https://www.traveldaily.cn/rss",
        # 品橙旅游（备用，WordPress RSS）
        "http://www.pinchain.com/feed/",
    ],
}
MAX_PER_CATEGORY = 4
FETCH_TIMEOUT = 6.0
SUMMARY_MAX_CHARS = 110

CATEGORY_LABELS = {"ai": "AI 圈", "travel": "旅文圈"}

_TAG_RE = re.compile(r"<[^>]+>")
# RSS2.0: <item><title/><link/><description/><pubDate/>；Atom: <entry><title/><link href/><summary/><updated/>
_TEXT_TAGS = ("title", "description", "summary", "content", "published", "updated", "pubDate")


def _clean_text(raw: str | None, limit: int = SUMMARY_MAX_CHARS) -> str:
    """去 HTML 标签/多余空白，截断到 limit。"""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _parse_feed(xml_text: str, category: str) -> list[dict[str, Any]]:
    """宽松解析 RSS/Atom，返回统一结构的条目列表（未排序未截断）。"""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    items: list[dict[str, Any]] = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in node:
            ctag = child.tag.rsplit("}", 1)[-1]
            if ctag == "link":
                # Atom 的 link 是自闭合带 href；RSS 是文本
                link = (child.get("href") or (child.text or "").strip()) or link
                continue
            if ctag in _TEXT_TAGS:
                fields[ctag] = "".join(child.itertext()).strip()
        title = _clean_text(fields.get("title"), limit=120)
        if not title or not link:
            continue
        summary = _clean_text(fields.get("description") or fields.get("summary") or fields.get("content"))
        published = fields.get("pubDate") or fields.get("published") or fields.get("updated") or ""
        # 来源域名作为媒体名兜底（RSS 一般不带源名）
        try:
            source = re.match(r"https?://([^/]+)", link).group(1).replace("www.", "")  # type: ignore[union-attr]
        except (AttributeError, TypeError):
            source = ""
        items.append(
            {
                "category": category,
                "title": title,
                "summary": summary,
                "source": source,
                "url": link,
                "published_at": published,
            }
        )
    return items


def _fetch_news() -> dict[str, list[dict[str, Any]]]:
    """抓全部源。任何失败都吞掉返回部分结果，空结果交给调用方走兜底。"""
    result: dict[str, list[dict[str, Any]]] = {}
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": "StaffDeck/1.0 (+plaza-news)"}) as client:
        for category, urls in FEEDS.items():
            collected: list[dict[str, Any]] = []
            seen: set[str] = set()
            for url in urls:
                try:
                    resp = client.get(url)
                    resp.raise_for_status()
                    entries = _parse_feed(resp.text, category)
                except (httpx.HTTPError, ElementTree.ParseError):
                    continue
                for entry in entries:
                    if entry["url"] in seen:
                        continue
                    seen.add(entry["url"])
                    collected.append(entry)
                    if len(collected) >= MAX_PER_CATEGORY:
                        break
                if len(collected) >= MAX_PER_CATEGORY:
                    break
            result[category] = collected
    return result


def _has_all_categories(items: dict[str, list[dict[str, Any]]]) -> bool:
    return all(items.get(category) for category in FEEDS)


# ---- 内置种子新闻（仅当外网抓取失败且无任何历史缓存时兜底展示）----
FALLBACK_ITEMS: dict[str, list[dict[str, Any]]] = {
    "ai": [
        {
            "category": "ai",
            "title": "国产大模型相继进入全球头部平台结算体系，出海模式迎质变",
            "summary": "智谱 GLM 接入 AWS Bedrock 按调用量分成，中国模型周调用量持续领跑全球。",
            "source": "证券之星",
            "url": "https://hk.stockstar.com/IG2026100800033345.shtml",
            "published_at": "2026-10-08",
        },
        {
            "category": "ai",
            "title": "GPT-6 全量上线：ChatGPT 新增「智能 UI」生成图表与交互组件",
            "summary": "AI 不再只输出文字，可按任务动态生成界面；小模型性价比之战同步开打。",
            "source": "凤凰网科技",
            "url": "https://ishare.ifeng.com/c/s/v006EWGTkqWjrPCq8uzmiXMZSkcda--dMta7J9rAlEUAUvL5Si--LDpJpfwsD--87wnL2sP",
            "published_at": "2026-10-08",
        },
        {
            "category": "ai",
            "title": "Claude Haiku 5.5 发布：API 价格降至上一代十分之一",
            "summary": "$0.10/M 输入 tokens，面向摘要、分类与高并发 Agent 场景的成本基线被再次拉低。",
            "source": "量子位",
            "url": "https://www.qbitai.com/",
            "published_at": "2026-10-08",
        },
        {
            "category": "ai",
            "title": "Google 开源多模态嵌入模型，可端侧运行",
            "summary": "把文本/图像/音频/视频映射到同一检索空间，本地语义搜索的基础设施级更新。",
            "source": "机器之心",
            "url": "https://www.jiqizhixin.com/",
            "published_at": "2026-10-06",
        },
    ],
    "travel": [
        {
            "category": "travel",
            "title": "智慧旅游加速落地：多家景区上线 AI 导览与行程规划",
            "summary": "大模型导览、多语种讲解与个性化行程规划成为景区数字化升级的标配方向。",
            "source": "行业动态",
            "url": "https://www.traveldaily.cn/",
            "published_at": "",
        },
        {
            "category": "travel",
            "title": "冰雪季临近，滑雪度假产品搜索热度持续走高",
            "summary": "冬季滑雪游进入预订高峰，一站式度假村与雪场配套服务竞争加剧。",
            "source": "行业动态",
            "url": "https://www.traveldaily.cn/",
            "published_at": "",
        },
        {
            "category": "travel",
            "title": "出入境游持续复苏，航司加密国际航线运力",
            "summary": "多家航司宣布新增与恢复国际航线，签证便利化政策带动出境游需求回暖。",
            "source": "行业动态",
            "url": "https://www.traveldaily.cn/",
            "published_at": "",
        },
        {
            "category": "travel",
            "title": "文旅消费新场景：AI 礼宾进入酒店与度假村服务链路",
            "summary": "从预订咨询到住中服务，AI 助手正在承接酒店业高频标准化客诉与咨询。",
            "source": "行业动态",
            "url": "https://www.traveldaily.cn/",
            "published_at": "",
        },
    ],
}


def _today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _row_payload(row: PlazaNewsCache) -> dict[str, Any]:
    return {
        "date": row.cache_date,
        "items": row.items_json,
        "provenance": row.provenance,
    }


def get_plaza_news_impl(db: Session, tenant_id: str) -> dict[str, Any]:
    """业务实现（与路由解耦，方便单测直调）。"""
    today = _today_str()

    # 1) 当天缓存命中
    row = db.exec(
        select(PlazaNewsCache).where(
            PlazaNewsCache.tenant_id == tenant_id,
            PlazaNewsCache.cache_date == today,
        )
    ).first()
    if row:
        return _row_payload(row)

    # 2) 抓一次并落缓存（部分类目失败也落，缺失类目由回退链补齐）
    fetched = _fetch_news()
    if _has_all_categories(fetched):
        row = PlazaNewsCache(
            tenant_id=tenant_id,
            cache_date=today,
            items_json=fetched,
            provenance="rss",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _row_payload(row)

    # 3) 回退最近一次缓存（任意日期）
    last = db.exec(
        select(PlazaNewsCache)
        .where(PlazaNewsCache.tenant_id == tenant_id)
        .order_by(PlazaNewsCache.cache_date.desc())  # type: ignore[attr-defined]
    ).first()
    if last:
        # 用最新缓存，但把缺失类目用种子补上，保证两类目都有内容
        items = dict(last.items_json or {})
        for category in FEEDS:
            if not items.get(category):
                items[category] = FALLBACK_ITEMS[category]
        return {"date": last.cache_date, "items": items, "provenance": last.provenance}

    # 4) 全兜底：种子新闻（仍然落当天缓存，避免今天反复构造）
    row = PlazaNewsCache(
        tenant_id=tenant_id,
        cache_date=today,
        items_json=FALLBACK_ITEMS,
        provenance="fallback",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _row_payload(row)


@router.get("", dependencies=[Depends(require_current_tenant)])
def get_plaza_news(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    """首页新闻：AI 圈 + 旅文圈，按天缓存，一日一更新。"""
    return get_plaza_news_impl(db, tenant_id)
