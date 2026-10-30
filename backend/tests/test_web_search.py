"""互联网检索模块的单元测试（不联网）。

被测语义全部来自现场问题，因此每条断言都对应一个**真实故障模式**：

1. 被反爬拦截必须报错，不能返回空列表（否则模型会答「未找到相关信息」）；
2. 页面可访问但选择器失配，要与「被拦」区分开（``WEB_SEARCH_PARSE_EMPTY``）；
3. 同一引擎内按 URL 去重、跨引擎也要去重；
4. 抖音 JSON 残片式的标题必须被过滤；
5. 引擎垂直频道（图片/文库/AI 问答）必须被过滤；
6. CJK 高亮空格要清掉，但英文词间空格必须保留；
7. 中转跳转链要还原，含 JS 跳转页这种不发 302 的形态。
"""

from __future__ import annotations

import httpx
import pytest

from app.web_search.engines import get_engines
from app.web_search.fetch import (
    clean_text,
    detect_block,
    is_channel_url,
    is_noise_title,
    normalize_url,
    resolve_redirect,
    site_of,
    unwrap_redirect,
)
from app.web_search.models import (
    WEB_SEARCH_ALL_ENGINES_FAILED,
    WEB_SEARCH_BLOCKED,
    WEB_SEARCH_EMPTY_QUERY,
    WebSearchError,
    WebSearchQuery,
)
from app.web_search.service import MIN_SUFFICIENT_HITS, search_web


class _FakeResponse:
    def __init__(self, text: str, *, url: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.url = httpx.URL(url or "https://example.invalid/")

    @property
    def headers(self) -> dict[str, str]:
        return {}


class _FakeClient:
    """替换 ``make_client``，按 URL 关键字返回预置页面。

    未预置的 URL 一律返回一个空页面而不是抛错 —— 检索前会先预热各引擎首页，
    预热不是被测行为，让每个用例都去登记首页只会淹掉真正的断言。
    """

    def __init__(self, routes: dict[str, _FakeResponse | Exception]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.requests.append(str(url))
        for marker, payload in self.routes.items():
            if marker in str(url):
                if isinstance(payload, Exception):
                    raise payload
                return payload
        return _FakeResponse("<html></html>", url=str(url))

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _bing_html(rows: list[tuple[str, str, str, str]]) -> str:
    """构造 Bing 结果页 HTML：``(title, href, snippet, date)``。"""

    blocks = []
    for title, href, snippet, date in rows:
        caption = f"{date}\u2002·\u2002{snippet}" if date else snippet
        blocks.append(
            '<li class="b_algo"><h2><a href="'
            + href
            + '">'
            + title
            + '</a></h2><div class="b_caption"><p>'
            + caption
            + "</p></div></li>"
        )
    return "<html><body><ol>" + "".join(blocks) + "</ol></body></html>"


@pytest.fixture()
def patch_client(monkeypatch: pytest.MonkeyPatch):
    def _install(routes: dict[str, _FakeResponse | Exception]) -> _FakeClient:
        client = _FakeClient(routes)
        monkeypatch.setattr(
            "app.web_search.service.make_client",
            lambda timeout_seconds: client,
        )
        return client

    return _install


def test_detect_block_recognises_baidu_captcha() -> None:
    """百度验证页 HTTP 200 但正文是「百度安全验证」，必须判为被拦。"""

    assert detect_block("<title>百度安全验证</title>") != ""
    assert detect_block("https://wappass.baidu.com/static/captcha/x.html") != ""
    assert detect_block("<title>打印机怎么用 - 百度</title>") == ""


def test_detect_block_recognises_sogou_antispider() -> None:
    """搜狗反爬页标题仍是「搜狗搜索」，只能靠正文里的 antispider 识别。"""

    assert detect_block("<title>搜狗搜索</title>/antispider/?m=1") != ""


def test_blocked_engine_raises_instead_of_empty_result(patch_client) -> None:
    """★核心语义：被反爬拦截必须抛错，绝不能返回「0 条结果」。"""

    patch_client(
        {
            "cn.bing.com": _FakeResponse("<title>百度安全验证</title>"),
            "baidu.com": _FakeResponse("<title>百度安全验证</title>"),
            "so.com": _FakeResponse("<title>百度安全验证</title>"),
            "sogou.com": _FakeResponse("<title>百度安全验证</title>"),
        }
    )
    with pytest.raises(WebSearchError) as excinfo:
        search_web(
            "打印机怎么用",
            engines=["bing"],
            inter_engine_delay_seconds=0,
        )
    assert excinfo.value.code == WEB_SEARCH_BLOCKED
    assert excinfo.value.retryable is True


def test_parse_empty_is_distinct_from_blocked(patch_client) -> None:
    """可访问但解析不出条目 → PARSE_EMPTY / ALL_FAILED，而不是 BLOCKED。"""

    patch_client({"cn.bing.com": _FakeResponse("<html><body>nothing</body></html>")})
    with pytest.raises(WebSearchError) as excinfo:
        search_web("打印机怎么用", engines=["bing"], inter_engine_delay_seconds=0)
    assert excinfo.value.code == WEB_SEARCH_ALL_ENGINES_FAILED
    assert [item["error_code"] for item in excinfo.value.details["attempts"] if item["engine"] == "bing"] == [
        "WEB_SEARCH_PARSE_EMPTY"
    ]


def test_empty_query_rejected() -> None:
    with pytest.raises(WebSearchError) as excinfo:
        search_web("   ")
    assert excinfo.value.code == WEB_SEARCH_EMPTY_QUERY


def test_bing_parse_extracts_real_url_snippet_and_date(patch_client) -> None:
    patch_client(
        {
            "cn.bing.com/search": _FakeResponse(
                _bing_html(
                    [
                        ("电脑怎么连接打印机", "https://www.zhihu.com/question/1", "先连接电源，再装驱动。", "2025年11月21日"),
                        ("打印机使用教程", "https://example.com/a", "第二步是放纸。", "6 天之前"),
                    ]
                )
            )
        }
    )
    result = search_web("打印机怎么用", engines=["bing"], max_results=5, inter_engine_delay_seconds=0)
    assert result.count == 2
    assert result.sufficient is False  # 只有 2 条 < 阈值
    assert result.hits[0].url == "https://www.zhihu.com/question/1"
    assert result.hits[0].published_at == "2025年11月21日"
    assert result.hits[0].snippet == "先连接电源，再装驱动。"
    assert result.hits[0].site == "zhihu.com"
    # 「6 天之前」这种相对时间也要能抽出来，不能按月日格式硬切。
    assert result.hits[1].published_at == "6 天之前"


def test_dedupe_by_normalised_url_across_engines(patch_client) -> None:
    """同一 URL（协议/www/尾斜杠不同）在不同引擎出现只保留一条。"""

    page = _bing_html(
        [
            ("电脑怎么连接打印机", "https://www.zhihu.com/question/1", "摘要一", ""),
            ("打印机使用教程", "https://example.com/a", "摘要二", ""),
            ("打印机怎么用", "https://example.com/b", "摘要三", ""),
        ]
    )
    patch_client(
        {
            "cn.bing.com/search": _FakeResponse(page),
            "baidu.com/s": _FakeResponse(
                '<div class="c-container"><h3><a href="x">重复条目</a></h3></div>'
                '<div class="c-container" mu="http://zhihu.com/question/1/">'
                '<h3><a href="/link?url=x">重复条目</a></h3>'
                '<span class="c-color-text">摘要</span></div>'
            ),
        }
    )
    result = search_web(
        "打印机怎么用",
        engines=["bing", "baidu_news"],
        max_results=10,
        inter_engine_delay_seconds=0,
    )
    urls = [hit.url for hit in result.hits]
    # zhihu.com/question/1 只出现一次（bing 版本），百度那条被去重。
    assert sum(1 for url in urls if "zhihu.com/question/1" in url) == 1
    assert "/link?url=x" not in " ".join(urls)


def test_json_fragment_title_filtered(patch_client) -> None:
    """百度资讯把抖音的 JSON 残片当标题，必须被过滤。"""

    patch_client(
        {
            "baidu.com/s": _FakeResponse(
                '<div class="c-container" mu="https://www.douyin.com/hashtag/1">'
                '<h3><a href="x">{"code":0,"stg_ve</a></h3></div>'
                '<div class="c-container" mu="https://example.com/ok">'
                '<h3><a href="x">打印机怎么用完整教程</a></h3>'
                '<span class="c-color-text">摘要正文</span></div>'
            )
        }
    )
    result = search_web("打印机怎么用", engines=["baidu_news"], inter_engine_delay_seconds=0)
    assert result.count == 1
    assert result.hits[0].url == "https://example.com/ok"


def test_channel_urls_and_image_titles_filtered(patch_client) -> None:
    """360 的图片/AI 频道页与「360 图片」标题都要丢。"""

    patch_client(
        {
            "so.com/s": _FakeResponse(
                '<ul>'
                '<li class="res-list"><h3 class="res-title"><a href="https://image.so.com/i?q=x">打印机怎么用_360图片</a></h3></li>'
                '<li class="res-list"><h3 class="res-title"><a href="https://ai.so.com/search/1">打印机怎么用使用教程</a></h3></li>'
                '<li class="res-list"><h3 class="res-title"><a href="https://example.com/keep">打印机怎么用步骤</a></h3>'
                '<p class="res-desc">先装驱动。</p></li>'
                "</ul>"
            )
        }
    )
    result = search_web("打印机怎么用", engines=["so360"], inter_engine_delay_seconds=0)
    assert [hit.url for hit in result.hits] == ["https://example.com/keep"]


def test_cjk_highlight_spaces_collapsed_but_english_kept() -> None:
    assert clean_text("打印机 怎么 用") == "打印机怎么用"
    assert clean_text("Staff Deck 与 python list") == "Staff Deck 与 python list"


def test_noise_title_detection() -> None:
    assert is_noise_title("打印机怎么用 - 百度图片") is True
    assert is_noise_title("大家还在搜") is True
    assert is_noise_title("打印机怎么用完整教程") is False
    assert is_noise_title('{"code":0') is True
    assert is_noise_title("...") is True


def test_channel_url_detection() -> None:
    assert is_channel_url("https://image.so.com/i?src=x") is True
    assert is_channel_url("https://wenku.so.com/d/1") is True
    assert is_channel_url("https://example.com/a") is False


def test_normalize_url_drops_internal_search_links() -> None:
    assert normalize_url("https://www.baidu.com/s?wd=x", "https://www.baidu.com/") == ""
    assert normalize_url("javascript:void(0)", "https://x.com/") == ""
    assert normalize_url("//example.com/a", "https://x.com/") == "https://example.com/a"
    assert normalize_url("/page", "https://example.com/dir/") == "https://example.com/page"


def test_unwrap_redirect_reads_embedded_target() -> None:
    assert (
        unwrap_redirect("https://x.com/jump?url=https%3A%2F%2Freal.example%2Fa")
        == "https://real.example/a"
    )
    assert unwrap_redirect("https://x.com/jump?url=abc") == ""


def test_resolve_redirect_handles_js_redirect_page() -> None:
    """搜狗 /link?url= 不发 302，返回 window.location.replace 小页面。"""

    page = (
        '<meta content="always" name="referrer">'
        '<script>window.location.replace("https://www.bilibili.com/video/BV1")</script>'
    )
    client = _FakeClient(
        {"sogou.com/link": _FakeResponse(page, url="https://www.sogou.com/link?url=abc")}
    )
    assert resolve_redirect(client, "https://www.sogou.com/link?url=abc") == (
        "https://www.bilibili.com/video/BV1"
    )


def test_resolve_redirect_handles_meta_refresh() -> None:
    page = (
        '<noscript><META http-equiv="refresh" content="0;URL=\'https://real.example/x\'">'
        "</noscript>"
    )
    client = _FakeClient(
        {"baidu.com/link": _FakeResponse(page, url="https://www.baidu.com/link?url=abc")}
    )
    assert resolve_redirect(client, "https://www.baidu.com/link?url=abc") == "https://real.example/x"


def test_resolve_redirect_ignores_non_redirector() -> None:
    client = _FakeClient({})
    assert resolve_redirect(client, "https://example.com/a") == ""


def test_site_of_strips_www() -> None:
    assert site_of("https://www.zhihu.com/question/1") == "zhihu.com"
    assert site_of("https://blog.csdn.net/a") == "blog.csdn.net"
    assert site_of("not-a-url") == ""


def test_payload_aligns_with_knowledge_search_shape(patch_client) -> None:
    """``to_payload`` 必须给出 chunks / evidence_pack，才能复用引用卡片链路。"""

    patch_client(
        {
            "cn.bing.com/search": _FakeResponse(
                _bing_html(
                    [
                        (f"结果标题{i}", f"https://example.com/{i}", f"摘要{i}", "")
                        for i in range(4)
                    ]
                )
            )
        }
    )
    result = search_web("打印机怎么用", engines=["bing"], max_results=4, inter_engine_delay_seconds=0)
    payload = result.to_payload()
    assert payload["result_count"] == 4
    assert payload["sufficient"] is True
    assert {item["source_type"] for item in payload["chunks"]} == {"web"}
    assert [item["chunk_id"] for item in payload["chunks"]] == ["web_1", "web_2", "web_3", "web_4"]
    assert len(payload["evidence_pack"]) == 4
    citations = result.to_citations()
    assert [item["index"] for item in citations] == [1, 2, 3, 4]
    assert citations[0]["url"] == "https://example.com/0"


def test_sufficient_threshold_boundary(patch_client) -> None:
    rows = [
        (f"标题内容{i}", f"https://example.com/{i}", "摘要内容够长可以支撑作答", "")
        for i in range(MIN_SUFFICIENT_HITS)
    ]
    patch_client({"cn.bing.com/search": _FakeResponse(_bing_html(rows))})
    result = search_web("打印机怎么用", engines=["bing"], max_results=8, inter_engine_delay_seconds=0)
    assert result.count == MIN_SUFFICIENT_HITS
    assert result.sufficient is True
    assert result.insufficient_reason == ""


def test_engine_registry_order_and_lookup() -> None:
    names = [spec.name for spec in get_engines()]
    assert names[0] == "bing"
    assert "baidu_news" in names
    assert [spec.name for spec in get_engines(["sogou", "nope"])] == ["sogou"]
    # 未知引擎名全部落空时回退默认顺序，而不是返回空列表导致「没有引擎可试」
    assert [spec.name for spec in get_engines(["nope"])][0] == "bing"


def test_search_web_accepts_query_object(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(
        {"cn.bing.com/search": _FakeResponse(_bing_html([("标题内容", "https://e.com/a", "摘要", "")]))}
    )
    monkeypatch.setattr("app.web_search.service.make_client", lambda timeout_seconds: client)
    result = search_web(WebSearchQuery(query="打印机怎么用", engines=["bing"], max_results=1))
    assert result.count == 1