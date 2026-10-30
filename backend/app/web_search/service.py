"""互联网检索编排：引擎降级 → 归一化去重 → 召回充分性判定。

设计要点（每一条都对应一个现场踩过的问题）：

1. **被反爬拦住不等于没有结果**。百度无 UA 请求会返回 HTTP 200 的验证页，
   旧实现 ``except Exception: return None`` 把这种情况变成空列表，模型据此
   回答「未找到相关信息」。现在一律抛 ``WebSearchError(WEB_SEARCH_BLOCKED)``。
2. **只在一个引擎拿到足够结果时才停**。降级条件是「失败」或「有效条数
   不足下限」，不是「一条都没有」——单条结果不足以支撑作答。
3. **去重按归一化 URL**，不按标题：同一篇文章在不同站点转载有不同标题，
   而同一 URL 的重复一定是重复。
4. **召回充分性单独判定并如实下传**。``sufficient=False`` 让调用方可以
   明确说「公开检索不足以回答，请补充 …」，而不是硬凑。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.web_search.engines import EngineSpec, get_engines
from app.web_search.fetch import (
    clean_text,
    detect_block,
    is_channel_url,
    is_noise_title,
    make_client,
    resolve_redirect,
    site_of,
)
from app.web_search.models import (
    WEB_SEARCH_ALL_ENGINES_FAILED,
    WEB_SEARCH_BLOCKED,
    WEB_SEARCH_EMPTY_QUERY,
    WEB_SEARCH_HTTP_ERROR,
    WEB_SEARCH_NETWORK_ERROR,
    WEB_SEARCH_PARSE_EMPTY,
    WebSearchError,
    WebSearchHit,
    WebSearchQuery,
    WebSearchResult,
)

#: 单次检索认为「够用」的最小有效条数。低于它继续降级到下一个引擎，
#: 全部引擎都试完仍不足则把 ``sufficient`` 置 False（结果照样返回）。
MIN_SUFFICIENT_HITS = 3

#: 摘要下限：短于它的条目说明只抓到了标题行，正文区选择器失配。
MIN_SNIPPET_CHARS = 24


def _dedupe_key(url: str) -> str:
    """去重键：忽略协议、www、末尾斜杠与常见追踪参数。"""

    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def _is_acceptable(hit: WebSearchHit) -> bool:
    if not hit.title or not hit.url:
        return False
    if is_noise_title(hit.title):
        return False
    if is_channel_url(hit.url):
        return False
    if len(hit.title) < 4:
        return False
    return True


def _collect(
    spec: EngineSpec,
    html: str,
    *,
    client: httpx.Client,
    query: WebSearchQuery,
    start_rank: int,
) -> list[WebSearchHit]:
    raw_hits = spec.parse(html, base_url=spec.base_url)
    collected: list[WebSearchHit] = []
    seen: set[str] = set()
    rank = start_rank
    for raw in raw_hits:
        title = clean_text(raw.title, limit=180)
        snippet = clean_text(raw.snippet, limit=520)
        url = raw.url
        if not url or not title:
            continue
        if is_noise_title(title) or is_channel_url(url):
            continue
        key = _dedupe_key(url)
        if key in seen:
            continue
        seen.add(key)
        collected.append(
            WebSearchHit(
                title=title,
                url=url,
                snippet=snippet,
                rank=rank,
                engine=spec.name,
                site=site_of(url),
                published_at=clean_text(raw.published_at, limit=32),
            )
        )
        rank += 1
    return collected


#: 中转链特征。百度普通结果是 ``baidu.com/link?url=``（按 host 判），
#: 搜狗是**相对路径** ``/link?url=``（按 path 判）——两种都要能还原。
_REDIRECT_HINTS = ("baidu.com/link", "sogou.com/link", "/link?url=", "link?url=")


def _fill_redirects(
    client: httpx.Client,
    hits: list[WebSearchHit],
    *,
    limit: int,
) -> None:
    """还原中转跳转链（原地改 ``url`` / ``site``）。

    还原失败时**保留原链接而不是丢弃条目**：点开跳转链仍能到达目标站，
    丢条目会让召回直接变少。
    """

    resolved = 0
    for hit in hits:
        if resolved >= limit:
            break
        if not any(marker in hit.url for marker in _REDIRECT_HINTS):
            continue
        target = resolve_redirect(client, hit.url)
        resolved += 1
        if target:
            hit.url = target
            hit.site = site_of(target)


def _snippet_for_fallback(hit: WebSearchHit) -> str:
    return hit.snippet or hit.title


def _warmup(client: httpx.Client, specs: list[EngineSpec]) -> str:
    """访问各引擎首页换 cookie，返回首个错误（全成功返回空串）。

    失败**不阻断**检索：某些引擎（Bing）本来就不需要 cookie，预热失败
    只损失那一个引擎，其余照常降级。
    """

    first_error = ""
    for spec in specs:
        if not spec.home_url:
            continue
        try:
            client.get(spec.home_url)
        except httpx.HTTPError as exc:
            if not first_error:
                first_error = f"{spec.name}: {type(exc).__name__}: {exc}"
    return first_error


def search_web(query: WebSearchQuery | str, **overrides: Any) -> WebSearchResult:
    """执行一次互联网检索。

    参数既接受 :class:`WebSearchQuery` 也接受裸字符串（公开 API 与 Harness
    两边调用形态不同，统一在这里兼容，避免上面各写一层适配）。
    """

    if isinstance(query, str):
        request = WebSearchQuery(query=query, **overrides)
    else:
        if overrides:
            raise TypeError("search_web 收到 WebSearchQuery 时不再接受额外参数")
        request = query

    text = (request.query or "").strip()
    if not text:
        raise WebSearchError(WEB_SEARCH_EMPTY_QUERY, "检索词不能为空。")

    max_results = max(1, min(int(request.max_results or 8), 20))
    specs = get_engines(request.engines)
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []
    merged: list[WebSearchHit] = []

    with make_client(request.timeout_seconds) as client:
        # 预热一次首页拿 cookie。实测这步是硬前提而非优化：
        # 同一 IP 上「直接搜」会被百度丢验证码页、被搜狗 302 到 /antispider/；
        # 先访问首页拿到 BAIDUID / SUID 等 cookie 后，两站都正常返回结果。
        warmup_error = _warmup(client, specs)
        if warmup_error:
            attempts.append(
                {
                    "engine": "_warmup",
                    "ok": False,
                    "error_code": WEB_SEARCH_NETWORK_ERROR,
                    "error": warmup_error,
                    "elapsed_ms": 0,
                }
            )
        for index, spec in enumerate(specs):
            if index > 0 and request.inter_engine_delay_seconds > 0:
                time.sleep(request.inter_engine_delay_seconds)
            attempt_started = time.monotonic()
            try:
                response = client.get(
                    spec.url(text, max_results),
                    headers={"Referer": spec.base_url},
                )
            except httpx.HTTPError as exc:
                attempts.append(
                    {
                        "engine": spec.name,
                        "ok": False,
                        "error_code": WEB_SEARCH_NETWORK_ERROR,
                        "error": f"{type(exc).__name__}: {exc}",
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    }
                )
                continue

            if response.status_code >= 400:
                attempts.append(
                    {
                        "engine": spec.name,
                        "ok": False,
                        "error_code": WEB_SEARCH_HTTP_ERROR,
                        "error": f"HTTP {response.status_code}",
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    }
                )
                continue

            html = response.text or ""
            block_marker = detect_block(html)
            if block_marker:
                attempts.append(
                    {
                        "engine": spec.name,
                        "ok": False,
                        "error_code": WEB_SEARCH_BLOCKED,
                        "error": f"命中反爬特征: {block_marker}",
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    }
                )
                continue

            hits = _collect(
                spec,
                html,
                client=client,
                query=request,
                start_rank=len(merged) + 1,
            )
            if not hits:
                attempts.append(
                    {
                        "engine": spec.name,
                        "ok": False,
                        "error_code": WEB_SEARCH_PARSE_EMPTY,
                        "error": "页面可访问但未解析出有效条目（选择器可能已失配）",
                        "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                    }
                )
                continue

            if request.resolve_redirects:
                _fill_redirects(client, hits, limit=min(len(hits), max_results))

            # 跨引擎去重：后面引擎补充的结果不能重复前面的。
            known = {_dedupe_key(item.url) for item in merged}
            fresh = [item for item in hits if _dedupe_key(item.url) not in known]
            for offset, item in enumerate(fresh):
                item.rank = len(merged) + offset + 1
            merged.extend(fresh)

            attempts.append(
                {
                    "engine": spec.name,
                    "ok": True,
                    "error_code": "",
                    "error": "",
                    "hit_count": len(fresh),
                    "elapsed_ms": int((time.monotonic() - attempt_started) * 1000),
                }
            )
            if len(merged) >= MIN_SUFFICIENT_HITS:
                break

    if not merged:
        # 全部引擎都没拿到东西：优先把「被拦」透出去，它比「解析空」更有指导性。
        blocked = [item for item in attempts if item.get("error_code") == WEB_SEARCH_BLOCKED]
        if blocked and len(blocked) == len(attempts):
            raise WebSearchError(
                WEB_SEARCH_BLOCKED,
                "检索请求被搜索引擎反爬拦截，请稍后重试或更换网络出口。",
                retryable=True,
                details={"attempts": attempts},
            )
        raise WebSearchError(
            WEB_SEARCH_ALL_ENGINES_FAILED,
            "所有检索引擎均未返回可用结果。",
            retryable=True,
            details={"attempts": attempts},
        )

    hits = merged[:max_results]
    sufficient = len(hits) >= MIN_SUFFICIENT_HITS
    insufficient_reason = ""
    if not sufficient:
        with_snippet = sum(
            1 for hit in hits if len(_snippet_for_fallback(hit)) >= MIN_SNIPPET_CHARS
        )
        insufficient_reason = (
            f"仅召回 {len(hits)} 条结果（阈值 {MIN_SUFFICIENT_HITS} 条），"
            f"其中 {with_snippet} 条带有效摘要；公开信息可能不足以支撑完整回答。"
        )

    return WebSearchResult(
        query=text,
        hits=hits,
        engine=hits[0].engine if hits else "",
        attempts=attempts,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        sufficient=sufficient,
        insufficient_reason=insufficient_reason,
    )