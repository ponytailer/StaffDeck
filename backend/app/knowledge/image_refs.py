"""回答正文里的图片引用：证据阶段给短标记，落地时拼回真实 URL。

为什么要有这一层（2026-11-13 重做）：

- 入库后 chunk 里是 ``![说明](kasset://kasset_xxx)``，**不放带签名的长 URL**：
  URL 约 66 字符，会被 900 字切块从中间切断，且写进正文只是噪声；
- 证据阶段把每个 kasset 按出现顺序编成 ``{{img:1}}``，图例（ref → 图题 → URL）
  放在证据包的 ``image_refs`` 里。**模型只复述 5 个字符，永远不碰 URL**——
  它抄错一个字符在旧方案里就是静默 403/404 裂图；
- 回答落地时（含 SSE 分片）换回 ``![说明](url)``，URL 由后端拼一次。

为什么不是 ``[[1]]``：``citations.py`` 的引用编号正则是
``\\[\\s*(\\d{1,3})\\](?![(:])``，匹配 ``[[1]]`` 时会在第二个 ``[`` 处命中并把
``[1]`` 当引用剥掉，剩 ``[]``。关掉「展示知识来源」时图片引用会集体消失。
``{{img:N}}`` 不含方括号，与引用编号、markdown 链接、链接定义都不冲突。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from app.knowledge.assets import asset_public_path

#: chunk 正文里的图片引用（入库时写入，资产 id 作定位符）。
IMAGE_PLACEHOLDER_PATTERN = re.compile(r"!\[([^\]]*)\]\(kasset://([A-Za-z0-9_]+)\)")

#: 给模型的短标记。
REF_PATTERN = re.compile(r"\{\{img:(\d{1,3})\}\}")

REF_TOKEN = "{{img:%d}}"

#: 单次回答最多带几张图。图例本身占预算，也防「证据里 20 张图」把上下文塞满。
MAX_IMAGE_REFS = 8

#: 代码围栏：围栏里的 ``{{img:1}}`` 是示例，不能替换。
CODE_FENCE_PATTERN = re.compile(r"(```|~~~)")

#: SSE 分片可能把 ``{{img:1}}`` 切成 ``{{`` + ``img:1}}``，尾部残片要先压住。
_PARTIAL_REF_TAIL_PATTERN = re.compile(r"\{\{(?:i(?:m(?:g(?::\d{0,2})?)?)?)?$")

#: 图题的展示兜底。
_DEFAULT_CAPTION = "原文档配图"

#: Word/WPS 没写图注时会自动塞 ``Picture 1`` / ``图片 2`` 这类占位串。
#: 直接当 alt 展示对用户没有信息量，还不如统一说「原文档配图」。
_CAPTION_PLACEHOLDER_PATTERN = re.compile(
    r"^(?:picture|image|图片|图)\s*\d+$", re.IGNORECASE
)


def _display_caption(alt: str) -> str:
    text = (alt or "").strip()
    if not text or _CAPTION_PLACEHOLDER_PATTERN.match(text):
        return _DEFAULT_CAPTION
    return text


def build_image_refs(payload: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """扫描证据，按出现顺序编号；返回（asset_id → N）与图例。

    编号在**裁剪之前**基于完整正文确定，所以后续按不同预算反复投影时编号不变——
    否则 ``_clip_keeping_images`` 把被截掉的图补到末尾会让同一张图换号，
    模型引用的 ``{{img:2}}`` 可能指向另一张图。
    """

    index: dict[str, int] = {}
    legend: list[dict[str, Any]] = []
    for entry in _iter_entries(payload):
        text = str(entry.get("content") or "")
        if "kasset://" not in text:
            continue
        for match in IMAGE_PLACEHOLDER_PATTERN.finditer(text):
            asset_id = match.group(2)
            if asset_id in index:
                continue
            if len(index) >= MAX_IMAGE_REFS:
                return index, legend
            number = len(index) + 1
            index[asset_id] = number
            legend.append(
                {
                    "ref": REF_TOKEN % number,
                    "caption": _display_caption(match.group(1)),
                    "url": asset_public_path(asset_id),
                    "source": _entry_source(entry),
                }
            )
    return index, legend


def rewrite_image_refs(text: Any, index: dict[str, int]) -> str:
    """把 chunk 里的 kasset 占位换成短标记；查不到编号时退化成图题文字。"""

    value = str(text or "")
    if not value or "kasset://" not in value:
        return value

    def _replace(match: re.Match[str]) -> str:
        number = index.get(match.group(2))
        if number is None:
            return _display_caption(match.group(1))
        return REF_TOKEN % number

    return IMAGE_PLACEHOLDER_PATTERN.sub(_replace, value)


def legend_urls(legend: Iterable[dict[str, Any]]) -> tuple[dict[int, str], dict[int, str]]:
    """图例 → （N → URL，N → 图题）。"""

    urls: dict[int, str] = {}
    captions: dict[int, str] = {}
    for item in legend:
        if not isinstance(item, dict):
            continue
        match = REF_PATTERN.fullmatch(str(item.get("ref") or ""))
        if match is None:
            continue
        number = int(match.group(1))
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        urls[number] = url
        captions[number] = str(item.get("caption") or "").strip() or _DEFAULT_CAPTION
    return urls, captions


def collect_legend(*payloads: Any) -> list[dict[str, Any]]:
    """从若干证据载荷里收集图例（Harness 路径的降级用）。"""

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for item in payload.get("image_refs") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(dict(item))
    return merged


class ImageRefExpander:
    """跨 SSE 分片把 ``{{img:N}}`` 换成 markdown 图片。

    与 ``citations.CitationMarkerStreamStripper`` 同构：压住半截标记、跨片保持
    围栏状态。不做跨片处理的话，``{{`` + ``img:1}}`` 会原样漏给用户。
    """

    def __init__(self, legend: Iterable[dict[str, Any]]) -> None:
        self._urls, self._captions = legend_urls(legend)
        self._pending = ""
        self._fence: str | None = None

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        text = self._pending + chunk
        text, self._pending = _split_partial_tail(text)
        return self._expand(text) if text else ""

    def flush(self) -> str:
        pending, self._pending = self._pending, ""
        return self._expand(pending) if pending else ""

    def _expand(self, text: str) -> str:
        if not text:
            return ""
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            if self._fence is not None:
                end = text.find(self._fence, cursor)
                if end < 0:
                    parts.append(text[cursor:])
                    break
                parts.append(text[cursor : end + 3])
                cursor = end + 3
                self._fence = None
                continue
            match = CODE_FENCE_PATTERN.search(text, cursor)
            if match is None:
                parts.append(_expand_segment(text[cursor:], self._urls, self._captions))
                break
            parts.append(_expand_segment(text[cursor : match.start()], self._urls, self._captions))
            parts.append(match.group(1))
            cursor = match.end()
            self._fence = match.group(1)
        return "".join(parts)


def expand_image_refs(
    text: str,
    legend: Iterable[dict[str, Any]],
) -> str:
    """整段正文的替换（非流式路径）。"""

    if not text or "{{img:" not in text:
        return text
    expander = ImageRefExpander(legend)
    return expander.feed(text) + expander.flush()


def strip_image_refs(text: str) -> str:
    """清掉没被替换的标记。

    用于「拿不到图例」的降级路径：宁可少一张图，也不能把 ``{{img:1}}`` 漏给用户。
    """

    if not text or "{{img:" not in text:
        return text
    return REF_PATTERN.sub("", text)


def build_image_appendix(
    legend: Iterable[dict[str, Any]],
    limit: int = 2,
) -> str:
    """模型一张图都没引用时，补在文末的「原文档配图」区块。

    返回空串表示没有可补的图，调用方据此决定要不要加分隔线。
    """

    picked: list[str] = []
    for item in legend:
        if not isinstance(item, dict):
            continue
        if len(picked) >= limit:
            break
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        caption = str(item.get("caption") or "").strip() or _DEFAULT_CAPTION
        picked.append(f"![{caption}]({url})")
    if not picked:
        return ""
    return "**原文档配图**\n\n" + "\n\n".join(picked)


def _expand_segment(
    text: str,
    urls: dict[int, str],
    captions: dict[int, str],
) -> str:
    if not text or "{{img:" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        number = int(match.group(1))
        url = urls.get(number)
        if not url:
            # 越界编号：模型编了个不存在的图，静默丢掉，不产生裂图
            return ""
        caption = captions.get(number) or _DEFAULT_CAPTION
        return f"![{caption}]({url})"

    return REF_PATTERN.sub(_replace, text)


def _split_partial_tail(text: str) -> tuple[str, str]:
    """拆出尾部可能被后续分片补全的残片。

    注意 ``start == 0`` 时必须**整段压住**：`{{` + `img` 两片拼起来正好是
    `{{img`，此时整个分片就是半截标记，先下发就等于把 ``{{img`` 漏给用户。
    """

    marker = _PARTIAL_REF_TAIL_PATTERN.search(text)
    if marker is not None:
        start = marker.start()
        return text[:start], text[start:]
    return text, ""


def _iter_entries(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("evidence_pack", "chunks", "selected_concepts"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                yield item


def _entry_source(entry: dict[str, Any]) -> str:
    for key in ("source_path", "document_title", "title", "section_path"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value[:80]
    return ""
