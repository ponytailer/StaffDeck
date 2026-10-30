"""互联网检索的结果模型与错误类型。

设计与既有 ``knowledge_search`` 保持同一套对外形状（``success`` / ``data`` /
``citations``），这样 Harness、前端引用卡片、公开 API 都不用为「网页结果」
再写一套分支。差异只在字段语义：

- ``chunk_id`` 对应网页条目 id（``web_1``…），不指向本地知识库分块；
- ``source_type="web"`` 让引用卡片可以区分「企业知识」与「互联网来源」；
- ``url`` 是真实可访问地址（百度跳转链已在检索层还原）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WebSearchError(RuntimeError):
    """检索失败的显式错误。

    为什么不用「返回空列表」表达失败：空列表会让调用方与模型都无法区分
    「搜索成功但没有结果」和「被反爬拦住了 / 网络不通 / 解析器失配」，
    现场表现为模型自信地回一句「未找到相关信息」。所有失败都必须带
    ``code`` 走到调用方，由它决定降级还是如实告知用户。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


# 错误码：大写常量便于调用方 switch，也便于日志检索。
WEB_SEARCH_EMPTY_QUERY = "WEB_SEARCH_EMPTY_QUERY"
WEB_SEARCH_BLOCKED = "WEB_SEARCH_BLOCKED"
WEB_SEARCH_NETWORK_ERROR = "WEB_SEARCH_NETWORK_ERROR"
WEB_SEARCH_HTTP_ERROR = "WEB_SEARCH_HTTP_ERROR"
WEB_SEARCH_PARSE_EMPTY = "WEB_SEARCH_PARSE_EMPTY"
WEB_SEARCH_ALL_ENGINES_FAILED = "WEB_SEARCH_ALL_ENGINES_FAILED"


@dataclass(slots=True)
class WebSearchHit:
    """单条检索结果（归一化后的统一形状）。"""

    title: str
    url: str
    snippet: str
    rank: int
    engine: str
    #: 站点域名（从 url 解析，失败时为空串），用于引用卡片展示来源站点。
    site: str = ""
    #: 发布时间文本（引擎给了才填），没有则为空串。
    published_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": f"web_{self.rank}",
            "title": self.title,
            "url": self.url,
            # 引用管线（knowledge_source_candidates）从 source_ref/uri 取
            # 来源路径；给上 url 才能在引用卡片里点回原文。
            "source_ref": self.url,
            "content": self.snippet,
            "score": round(1.0 / self.rank, 4) if self.rank else 0.0,
            "source_type": "web",
            "engine": self.engine,
            "site": self.site,
            "published_at": self.published_at,
            "rank": self.rank,
        }


@dataclass(slots=True)
class WebSearchQuery:
    """一次检索请求。"""

    query: str
    max_results: int = 8
    #: 只使用指定引擎（按名字），用于诊断；默认按配置顺序全部尝试。
    engines: list[str] | None = None
    timeout_seconds: float = 12.0
    #: 引擎之间的额外间隔（秒）。全部失败才会串行换下一个引擎，
    #: 正常只请求一个引擎，间隔只在降级时生效。
    inter_engine_delay_seconds: float = 0.8
    #: 是否解析跳转链接。百度普通结果给的是 ``baidu.com/link?url=``，
    #: 需要二次请求还原；关掉能省一次往返但引用里就只有跳转链。
    resolve_redirects: bool = True


@dataclass(slots=True)
class WebSearchResult:
    """检索成功结果。"""

    query: str
    hits: list[WebSearchHit] = field(default_factory=list)
    engine: str = ""
    #: 尝试过的引擎及其结果（成功/失败原因），用于诊断与 trace。
    attempts: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    #: 是否属于「召回不足以支撑作答」——由 ``is_sufficient`` 判定，
    #: 调用方据此决定是否如实告知用户而不是硬答。
    sufficient: bool = True
    insufficient_reason: str = ""

    @property
    def count(self) -> int:
        return len(self.hits)

    def to_payload(self) -> dict[str, Any]:
        """转成与 ``knowledge_search`` 对齐的 ``data`` 形状。"""

        chunks = [hit.to_dict() for hit in self.hits]
        return {
            "chunks": chunks,
            "evidence_pack": [
                {
                    "chunk_id": item["chunk_id"],
                    "title": item["title"],
                    "url": item["url"],
                    "source_ref": item["url"],
                    "content": item["content"],
                    "source_type": "web",
                    "site": item["site"],
                }
                for item in chunks
            ],
            "query": self.query,
            "engine": self.engine,
            "result_count": len(chunks),
            "sufficient": self.sufficient,
            "insufficient_reason": self.insufficient_reason,
            "engine_attempts": list(self.attempts),
        }

    def to_citations(self) -> list[dict[str, Any]]:
        """转成引用卡片。

        注意 ``index`` 从 1 连续编号 —— 正文里的 ``[1] [2]`` 由模型按下标写，
        错位就会出现「引用了不存在的来源」。
        """

        citations: list[dict[str, Any]] = []
        for offset, hit in enumerate(self.hits, start=1):
            citations.append(
                {
                    "index": offset,
                    "chunk_id": f"web_{hit.rank}",
                    "title": hit.title,
                    "url": hit.url,
                    "snippet": hit.snippet[:200],
                    "source_type": "web",
                    "site": hit.site,
                    "engine": hit.engine,
                }
            )
        return citations