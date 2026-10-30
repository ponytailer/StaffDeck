"""抓页面的公共基础设施：HTTP 客户端、反爬识别、URL 归一化、正文清洗。

这里的东西都是「所有引擎共用」的判定与清洗逻辑。引擎解析器只负责把
DOM 变成 ``(title, url, snippet)`` 三元组，其它一律交给本模块，
避免每个引擎各写一套「什么算被拦截」而口径漂移。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

# 桌面 UA：百度对「无 UA / 非浏览器 UA」的请求直接返回验证码页，
# 而验证码页同样是 HTTP 200 —— 只能靠正文特征识别。
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": DESKTOP_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

# 反爬/验证页特征。命中任一即判定被拦截，**绝不能**当成「搜索结果为空」。
# 都是各站验证页标题或在 HTML 里出现的稳定标记。
_BLOCK_MARKERS = (
    "百度安全验证",
    "wappass.baidu.com",
    "static/captcha",
    "请输入验证码",
    "网络不给力，请稍后重试",
    "安全验证",
    "访问验证",
    "/sorry/",
    "unusual traffic",
    "verify you are human",
    "are you a robot",
    "cf-browser-verification",
    "challenge-platform",
    # 搜狗无 cookie 直接搜会 302 到 /antispider/（页面标题仍是「搜狗搜索」，
    # 不含任何中文验证字样，只能靠 URL 与关键字识别）。
    "antispider",
    "/antip?",
    "antip=web",
)

# 段落间空白压缩：搜索引擎的摘要里常带 &#12288;（全角空格）、\u2002、
# 以及被拆开的 ``打印机`` 高亮标签，不压就会得到「打 印 机 怎 么 用」。
_WHITESPACE_RE = re.compile(r"[\s\u3000\u2002\u2003\u00a0\u200c\u200b]+")
# 中日韩字符之间的空格：命中词高亮 `<em>打印机</em>怎么用` 经 get_text(" ")
# 会变成「打印机 怎么用」。只压 CJK↔CJK 之间的空格，绝不动英文词间空格
# （否则「Staff Deck」会被粘成「StaffDeck」、「python list」会丢分隔）。
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\uff00-\uffef"
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])[ \t]+(?=[{_CJK}])")
# 摘要里混进来的 JSON/JS 片段：``{"code":0,"stg_ve...`` 之类。
_JSON_FRAGMENT_RE = re.compile(r"[{]\s*\"[^}]{0,200}[}]")
# 纯装饰性/无信息量的标题（引擎把图片模块、AI 卡片当结果返回）。
_NOISE_TITLE_RE = re.compile(
    r"(百度图片|图片大全|相关搜索|大家还在搜|正在思考|内容由AI生成|"
    r"AI正在分析|点击查看|展开全部|广告|推广)",
)

# 垃圾标题判定的**第二个信号**：不含任何中文且不含字母数字。
# 百度资讯会把抖音的 JSON 片段当标题吐出来（``{"code":0,"stg_ve...``），
# 它既不含噪声词也不违规，只能靠「没有任何语义字符」识别。
_MEANINGFUL_CHAR_RE = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]")
# 标题若是 JSON/JS 残片：以 ``{``/``[`` 开头，或以 ``}``/``]``/`,`/`:`/`"`` 收尾。
_CODE_FRAGMENT_RE = re.compile(r"^[\{\[]|[\}\],:]\s*$")

# 引擎把「自家垂直频道」当结果塞进来：360 的图片/文库/AI 问答、搜狗的视频/图片。
# 点开是频道页而非内容页，与前面「站内搜索链接」同类，一并丢弃。
_CHANNEL_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)*(?:"
    r"image\.so\.com|wenku\.so\.com|ai\.so\.com|v\.so\.com|"
    r"image\.sogou\.com|pic\.sogou\.com|v\.sogou\.com|"
    r"image\.baidu\.com|v\.baidu\.com"
    r")/",
    re.IGNORECASE,
)

# 站内/无效链接：命中即丢弃（它们点开不是内容页）。
_INVALID_URL_PREFIXES = ("javascript:", "mailto:", "tel:", "#")
_INTERNAL_HOST_MARKERS = (
    "baidu.com/s?",
    "baidu.com/from=",
    "so.com/s?",
    "sogou.com/web?",
)


def make_client(timeout_seconds: float) -> httpx.Client:
    """统一的 HTTP 客户端。

    ``trust_env=True`` 是刻意的：企业内网部署常配 ``HTTPS_PROXY`` 走 egress
    代理，读环境变量才能复用那套配置；直连可用的环境不受影响。
    """

    return httpx.Client(
        timeout=httpx.Timeout(timeout_seconds, connect=min(6.0, timeout_seconds)),
        follow_redirects=True,
        headers=dict(BASE_HEADERS),
        trust_env=True,
    )


def detect_block(html: str) -> str:
    """返回命中的反爬特征；空串表示正常页面。"""

    if not html:
        return "empty_body"
    head = html[:20000].lower()
    for marker in _BLOCK_MARKERS:
        if marker.lower() in head:
            return marker
    return ""


def clean_text(value: str, *, limit: int = 0) -> str:
    """清洗一段 HTML 文本：去 JSON 碎片、压空白、去零宽字符。"""

    text = _JSON_FRAGMENT_RE.sub(" ", value or "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _CJK_SPACE_RE.sub("", text)
    # 只剥**拼接用的分隔符**（引擎常把「摘要 · 站点名」拼在一起）。
    # 句号/省略号是正文的一部分，剥掉会让摘要看起来不完整，不能动。
    text = text.strip(" ·|—–-,，;；:：")
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def is_noise_title(title: str) -> bool:
    """标题是否属于「不是内容的标题」。

    三条独立判据，命中任一即噪声：

    - 命中噪声词表（图片模块 / 相关搜索 / AI 卡片提示语）；
    - 没有任何语义字符（纯标点/符号）；
    - 是 JSON/JS 残片（百度资讯里抖音条目的标题就是这种）。
    """

    if not title:
        return True
    if _NOISE_TITLE_RE.search(title):
        return True
    if not _MEANINGFUL_CHAR_RE.search(title):
        return True
    return bool(_CODE_FRAGMENT_RE.search(title))


def is_channel_url(url: str) -> bool:
    """是否是引擎自家的垂直频道页（图片/文库/视频/AI 问答）。"""

    return bool(_CHANNEL_URL_RE.match(url or ""))


def normalize_url(href: str, base: str) -> str:
    """把引擎给的 href 归一化成绝对地址；无效返回空串。"""

    raw = (href or "").strip()
    if not raw or raw.lower().startswith(_INVALID_URL_PREFIXES):
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    absolute = urljoin(base, raw)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    # 站内搜索链接不是内容页（百度把「相关搜索」也塞进结果列表）。
    lowered = absolute.lower()
    for marker in _INTERNAL_HOST_MARKERS:
        if marker in lowered:
            return ""
    return absolute


def site_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def unwrap_redirect(href: str) -> str:
    """从「中转跳转链」里直接取出真实地址。

    百度普通结果的 ``href`` 是 ``https://www.baidu.com/link?url=<base64ish>``，
    这类必须发请求才能还原（见 :func:`resolve_redirect`）。但也有不少引擎
    把真实地址放在查询参数里，这种能零成本取出，先试这一层。
    """

    try:
        parsed = urlparse(href)
    except ValueError:
        return ""
    query = parse_qs(parsed.query)
    for key in ("url", "u", "target", "to", "redirect", "link"):
        values = query.get(key) or []
        for value in values:
            candidate = unquote(value)
            if candidate.startswith(("http://", "https://")):
                return candidate
    return ""


# JS / meta 跳转页：搜狗的 ``/link?url=`` 不发 302，而是返回一个只有
# ``window.location.replace("真实地址")`` 的小页面（外加 noscript 里的
# meta refresh）。只看响应头的最终 URL 会永远停在跳转链上。
_JS_REDIRECT_RE = re.compile(
    r"""(?:window\.)?location(?:\.replace|\.href|\.assign)?\s*(?:=|\()\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_META_REFRESH_RE = re.compile(
    # content 的实际写法差异很大：``0;URL=https://x``、``0;URL='https://x'``、
    # ``0; url="https://x"``。两条经验：
    # 1) 不能用 ``[^>]*?url=`` 做懒匹配 —— 引号也是「非 >」字符，懒匹配会
    #    一路滑到属性末尾才回头，导致 ``content=`` 之后完全对不上；
    # 2) 捕获组要允许引号，URL 被单引号包住时（`content="0;URL='...'"`）
    #    排掉引号会一个字符都取不到。引号在取值后剥。
    r"""<meta[^>]*http-equiv=['"]?refresh['"]?[^>]*content=['"]?[^>]*url=\s*['"]?([^"'>\s]+)""",
    re.IGNORECASE,
)


def _looks_like_redirector(url: str) -> bool:
    lowered = (url or "").lower()
    return any(
        marker in lowered
        for marker in ("baidu.com/link?", "sogou.com/link?", "so.com/link?", "/link?url=")
    )


def resolve_redirect(client: httpx.Client, href: str) -> str:
    """还原中转跳转链，失败返回空串（调用方保留原链接）。"""

    if not href:
        return ""
    if not _looks_like_redirector(href):
        return ""
    direct = unwrap_redirect(href)
    if direct:
        return direct
    try:
        response = client.get(href, follow_redirects=True)
    except httpx.HTTPError:
        return ""
    final = str(response.url)
    # 只有跳出跳转链、且落在真实站点上才算还原成功。
    # 判据是「final 是否仍是跳转链」而不是「是否命中站内搜索特征」——
    # 搜狗的 ``/link?url=`` 不匹配 ``sogou.com/web?``，用后者判会漏。
    if not _looks_like_redirector(final) and final.startswith(("http://", "https://")):
        return final
    return _redirect_from_body(response.text)


def _redirect_from_body(html: str) -> str:
    """从跳转页正文里取出真实地址（JS location / meta refresh）。"""

    head = (html or "")[:8000]
    for pattern in (_JS_REDIRECT_RE, _META_REFRESH_RE):
        match = pattern.search(head)
        if not match:
            continue
        candidate = match.group(1).strip().strip("'\"")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith(("http://", "https://")):
            return candidate
    return ""