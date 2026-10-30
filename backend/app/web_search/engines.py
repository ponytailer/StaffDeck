"""各搜索引擎的 HTML 解析器。

**2026-10 实测结论（决定本模块形状）**：

- ``www.baidu.com/s?wd=...`` 普通结果页已改造成前端渲染 —— 服务端返回的
  HTML 里既没有 ``content_left`` 也没有 ``c-container``（旧 ``baidusearch``
  包按这两个选择器抓，所以只能抓到 0~2 条图片模块）。唯一仍是**服务端渲染**
  的百度入口是资讯频道 ``tn=news``，``div.c-container`` + ``h3 a`` + ``mu``
  属性直接给出标题/真实 URL/摘要，不需要还原 ``link?url=`` 跳转。
- Bing 中国站 (``cn.bing.com``) 是最稳的一路：静态渲染、0.3s、10 条结果、
  ``h2 a`` 直接是真实 URL，摘要带发布时间。
- 360 / 搜狗可用但要过滤两类垃圾：360 把 AI 卡片（``ai.so.com``）混进结果；
  搜狗的「大家还在搜」是无标题聚合块，且部分结果仍是 ``/link?url=`` 中转链。

因此解析器统一契约：**只产出候选条目，全部有效性判定（噪声标题、无效
URL、去重）由 ``service`` 统一做** —— 各引擎自查一遍必然出现口径漂移。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from app.web_search.fetch import clean_text, normalize_url


@dataclass(slots=True)
class RawHit:
    """解析器的原始产出：还没做有效性校验。"""

    title: str
    url: str
    snippet: str
    published_at: str = ""


class EngineSpec:
    """引擎解析器基类。

    刻意**不用 dataclass**：子类只覆写 ``name``/``label``/``base_url`` 三个
    类属性并实现 ``parse``，没有实例状态。dataclass 会把这几个类属性当字段，
    子类仅赋值不给注解时 ``__init__`` 仍要求传参，反而更绕。
    """

    name: str = ""
    label: str = ""
    base_url: str = ""
    #: 预热用的首页地址。不带 cookie 直接搜会被反爬拦（百度丢验证码页、
    #: 搜狗 302 到 /antispider/），先访问这个地址换 cookie。
    home_url: str = ""

    def params(self, query: str, max_results: int) -> dict[str, str]:
        raise NotImplementedError

    def parse(self, html: str, *, base_url: str) -> list[RawHit]:
        raise NotImplementedError

    def url(self, query: str, max_results: int) -> str:
        return f"{self.base_url}?{urlencode(self.params(query, max_results))}"


class BingEngine(EngineSpec):
    name = "bing"
    label = "Bing"
    base_url = "https://cn.bing.com/search"
    home_url = "https://cn.bing.com/"

    def params(self, query: str, max_results: int) -> dict[str, str]:
        return {"q": query, "setlang": "zh-hans", "count": str(min(max_results, 20))}

    def parse(self, html: str, *, base_url: str) -> list[RawHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[RawHit] = []
        for item in soup.select("li.b_algo"):
            anchor = item.select_one("h2 a")
            if anchor is None:
                continue
            title = clean_text(anchor.get_text(" ", strip=True))
            url = normalize_url(anchor.get("href") or "", base_url)
            caption = item.select_one("div.b_caption")
            body = caption.select_one("p") if caption is not None else None
            snippet = clean_text((body or caption).get_text(" ", strip=True)) if (body or caption) else ""
            # Bing 摘要前缀是「2025年11月21日 ·」/「6 天之前 ·」。按分隔符切，
            # 不能靠固定截断（「天前」只有两个字，按月日切会切空）。
            published = ""
            head, sep, tail = snippet.partition("·")
            if sep and len(head) <= 16 and any(
                token in head
                for token in (
                    "年",
                    "天前",
                    "天之前",
                    "小时前",
                    "分钟前",
                    "昨天",
                    "前天",
                    "刚刚",
                )
            ):
                published = head.strip()
                snippet = tail.strip()
            if not snippet:
                snippet = clean_text(item.get_text(" ", strip=True), limit=220)
            hits.append(RawHit(title=title, url=url, snippet=snippet, published_at=published))
        return hits


class BaiduNewsEngine(EngineSpec):
    """百度资讯频道（``tn=news``）：百度唯一仍然服务端渲染的结果入口。"""

    name = "baidu_news"
    label = "百度资讯"
    base_url = "https://www.baidu.com/s"
    home_url = "https://www.baidu.com/"

    def params(self, query: str, max_results: int) -> dict[str, str]:
        return {"wd": query, "tn": "news", "rn": str(min(max_results, 20))}

    def url(self, query: str, max_results: int) -> str:
        return super().url(query, max_results)

    def parse(self, html: str, *, base_url: str) -> list[RawHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[RawHit] = []
        for item in soup.select("div.c-container"):
            anchor = item.select_one("h3 a")
            if anchor is None:
                continue
            # ``mu`` 属性是百度自己给的**真实目标地址**，比 href 可靠：
            # href 有时是 ``/link?url=`` 中转链，``mu`` 一直是落地页。
            url = normalize_url(item.get("mu") or anchor.get("href") or "", base_url)
            title = clean_text(anchor.get_text(" ", strip=True))
            body = (
                item.select_one("span.c-color-text")
                or item.select_one(".c-abstract")
                or item.select_one("div.c-span-last")
            )
            snippet = clean_text(body.get_text(" ", strip=True)) if body is not None else ""
            published = ""
            poster = item.select_one(".c-color-gray2, .c-gap-right-xsmall")
            if poster is not None:
                published = clean_text(poster.get_text(" ", strip=True), limit=32)
            hits.append(RawHit(title=title, url=url, snippet=snippet, published_at=published))
        return hits


class So360Engine(EngineSpec):
    name = "so360"
    label = "360 搜索"
    base_url = "https://www.so.com/s"
    home_url = "https://www.so.com/"

    def params(self, query: str, max_results: int) -> dict[str, str]:
        return {"q": query, "pn": "1"}

    def parse(self, html: str, *, base_url: str) -> list[RawHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[RawHit] = []
        for item in soup.select("li.res-list"):
            anchor = item.select_one("h3.res-title a") or item.select_one("h3 a")
            if anchor is None:
                continue
            title = clean_text(anchor.get_text(" ", strip=True))
            url = normalize_url(anchor.get("href") or "", base_url)
            body = (
                item.select_one("p.res-desc")
                or item.select_one("div.res-desc")
                or item.select_one(".res-comm-con")
            )
            snippet = clean_text(body.get_text(" ", strip=True)) if body is not None else ""
            hits.append(RawHit(title=title, url=url, snippet=snippet))
        return hits


class SogouEngine(EngineSpec):
    name = "sogou"
    label = "搜狗"
    base_url = "https://www.sogou.com/web"
    home_url = "https://www.sogou.com/"

    def params(self, query: str, max_results: int) -> dict[str, str]:
        return {"query": query}

    def parse(self, html: str, *, base_url: str) -> list[RawHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[RawHit] = []
        for item in soup.select("div.vrwrap"):
            anchor = item.select_one("h3.vr-title a") or item.select_one("h3 a")
            if anchor is None:
                # 「大家还在搜」这类聚合块没有 h3，直接跳过。
                continue
            title = clean_text(anchor.get_text(" ", strip=True))
            href = anchor.get("href") or ""
            # 搜狗的 ``/link?url=`` 是相对路径，normalize_url 会补成绝对地址；
            # 真实落地页在 service 层用 resolve_redirect 还原。
            url = normalize_url(href, base_url)
            body = (
                item.select_one("div.text-layout")
                or item.select_one("div.str-text")
                or item.select_one(".space-txt")
            )
            snippet = clean_text(body.get_text(" ", strip=True)) if body is not None else ""
            hits.append(RawHit(title=title, url=url, snippet=snippet))
        return hits


#: 引擎注册表。顺序即**默认降级顺序**：质量最高的排前面，前一个失败/空
#: 才降级到下一个。任何新增引擎必须在这里登记并在 settings 里给默认值。
ENGINES: dict[str, EngineSpec] = {
    BingEngine.name: BingEngine(),
    BaiduNewsEngine.name: BaiduNewsEngine(),
    So360Engine.name: So360Engine(),
    SogouEngine.name: SogouEngine(),
}

DEFAULT_ENGINE_ORDER = ("bing", "baidu_news", "so360", "sogou")


def get_engines(names: list[str] | None = None) -> list[EngineSpec]:
    """按名字取引擎；未知名字忽略。``None`` 表示默认全部顺序。"""

    if not names:
        return [ENGINES[name] for name in DEFAULT_ENGINE_ORDER if name in ENGINES]
    picked: list[EngineSpec] = []
    for name in names:
        spec = ENGINES.get(str(name).strip().lower())
        if spec is not None and spec not in picked:
            picked.append(spec)
    return picked or [ENGINES[name] for name in DEFAULT_ENGINE_ORDER if name in ENGINES]