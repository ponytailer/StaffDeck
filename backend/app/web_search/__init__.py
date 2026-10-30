"""互联网检索（web search）：多引擎 HTML 抓取 + 归一化 + 反爬识别。

对外只暴露 :func:`search_web` 与 :class:`WebSearchResult`。
"""

from app.web_search.models import (
    WebSearchError,
    WebSearchHit,
    WebSearchQuery,
    WebSearchResult,
)
from app.web_search.service import search_web

__all__ = [
    "WebSearchError",
    "WebSearchHit",
    "WebSearchQuery",
    "WebSearchResult",
    "search_web",
]