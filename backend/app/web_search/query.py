"""把用户口语问句改写成搜索引擎能用的关键词。

## 为什么需要这一层

``search_web`` 原来把 ``message[:200]`` 原样丢给引擎。搜索引擎不解析句式，
只做关键词匹配，于是「今天的新闻有哪些」被拆成「今天」+「有哪些」两个词，
实测 bing 返回的前 8 条是日历网、老黄历、历史上的今天——模型据此回答
「今天是 2026 年第 299 天，万圣节还有 2 天」，检索白跑一趟。

同一个 query 改写成「今日要闻 2026年10月30日」后，返回的是光明日报要闻版、
每日 60 秒新闻早报、新华社每日电讯。

## 两条规则，边界由实测确定

1. **剥掉问句壳**（前缀「帮我查一下」「请问」、尾巴「有哪些」「是什么」「吗」）。
   任何 query 都做，安全无副作用：「报价单模板在哪下载」→「报价单模板」。

2. **纯泛新闻问句才补日期**，归一成「今日要闻 YYYY年M月D日」。
   **带实词的 query 绝不能补日期** —— 实测「新能源汽车补贴政策 2026年10月30日」
   会被切成「新（汉语汉字）_百度百科」「新浪网」「汉典」，而原样检索返回的是
   中国政府网的政策原文。日期只对「今天有什么新闻」这类没有主题词的问句有意义。
"""

from __future__ import annotations

import re
from datetime import date

#: 问句前缀：用户习惯的祈使/礼貌开头，不带任何检索语义。
_LEAD_PHRASES = (
    "帮我查一下",
    "帮我查查",
    "帮我搜一下",
    "帮我搜搜",
    "帮我看看",
    "帮忙查一下",
    "请帮我查一下",
    "我想知道一下",
    "我想问一下",
    "我想知道",
    "我想问",
    "请问一下",
    "请你",
    "帮我",
    "请帮我",
    "麻烦你",
    "麻烦",
    "请",
    "查一下",
    "查查",
    "搜一下",
    "搜搜",
    "搜索一下",
    "告诉我",
    "问一下",
)

#: 问句尾巴：按长度降序匹配，长尾巴优先（「有哪些」必须先于「哪些」）。
_TAIL_PHRASES = (
    "都有一些什么",
    "都有哪些",
    "有哪些呢",
    "有什么呢",
    "是多少呢",
    "是多少",
    "是哪些",
    "是什么",
    "是什么样",
    "怎么样呢",
    "怎么样",
    "怎么办",
    "怎么回事",
    "怎么走",
    "怎么弄",
    "怎么弄呢",
    "多少钱",
    "多少个",
    "有哪些",
    "有什么",
    "多少",
    "哪些",
    "什么",
    "如何",
    "怎么",
    "可以吗",
    "行吗",
    "吗",
    "呢",
    "啊",
    "呀",
    "吧",
    "？",
    "?",
    "。",
    "，",
    ",",
    "!",
    "！",
    "~",
    " ",
)

#: 泛新闻问句的构成 token。原文里的这些词全部抵消后若无残留，
#: 就说明这是一句「时间 + 泛新闻」的裸问句，没有主题词可检索。
_GENERIC_TOKENS = (
    # 时间
    "今天",
    "今日",
    "昨天",
    "昨日",
    "本周",
    "这周",
    "这个星期",
    "最近",
    "近期",
    "现在",
    "目前",
    "当前",
    "最新",
    # 泛新闻词（必须与疑问词完全抵消才算「裸问句」，
    # 所以「大新闻」里的「大」是不带语义的修饰词，一并列出）
    "新闻",
    "要闻",
    "资讯",
    "热点",
    "热榜",
    "消息",
    "快讯",
    "动态",
    "大事",
    "新鲜事",
    # 疑问/虚词/无信息量的修饰
    "有什么",
    "有哪些",
    "有哪些呢",
    "都有一些什么",
    "来",
    "有没有",
    "没有",
    "没什么",
    "没",
    "大",
    "一些",
    "一下",
    "的",
    "有",
    "了",
    "些",
    "都",
    "是",
    "什么",
    "哪些",
    "呢",
    "吗",
    "啊",
    "呀",
    "新",
    "发生",
    "值得",
    "关注",
)

#: 泛新闻问句里**必须命中**的锚点词。只靠「时间词 + 虚词」抵消成空
#: 就归一成新闻是危险的：单独一个「什么」「现在」也会被判成裸问句，
#: 于是用户问「什么」也会去搜今天新闻。必须有真实新闻类词。
_GENERIC_NEWS_ANCHORS = ("新闻", "要闻", "资讯", "热点", "热榜", "快讯", "大事", "新鲜事")

#: 泛新闻问句归一后的模板。实测「今日要闻 + 日期」的召回质量最好：
#: 「今日新闻」会返回站点首页（今日头条/央视网首页），加日期后才是新闻正文页。
_GENERIC_NEWS_TEMPLATE = "今日要闻 {year}年{month}月{day}日"

#: 引擎对超长 query 的切词质量明显下降，统一截断。
_MAX_QUERY_CHARS = 60

#: 剥壳后的最小可接受长度。太短说明原文本身就是短语，不要再动它。
_MIN_TRIMMED_CHARS = 2

_WHITESPACE = re.compile(r"\s+")


def _strip_lead(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for phrase in _LEAD_PHRASES:
            if text.startswith(phrase) and len(text) > len(phrase):
                text = text[len(phrase) :].lstrip(" ，,、:：")
                changed = True
                break
    return text


def _strip_tail(text: str) -> str:
    changed = True
    while changed:
        changed = False
        for phrase in _TAIL_PHRASES:
            if text.endswith(phrase) and len(text) > len(phrase):
                text = text[: -len(phrase)].rstrip(" ，,、:：")
                changed = True
                break
    return text


def _collapse_spaces(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def is_generic_news_query(text: str) -> bool:
    """是否是「时间 + 泛新闻」的裸问句（没有主题词）。

    两个条件同时成立才算：
    ① 原文含真实新闻类锚点词（新闻/要闻/热点…）；
    ② 把时间词、新闻词、疑问虚词全部抵消后没有残留。

    只做 ② 是错的：单独一个「什么」「现在」抵消后也是空，
    于是用户问「什么」也会被拉去搜今天新闻。
    """

    raw = _collapse_spaces(text or "")
    if not raw:
        return False
    if not any(anchor in raw for anchor in _GENERIC_NEWS_ANCHORS):
        return False

    residue = raw
    # 长 token 先抵消，避免「有哪些」被「些」「有」拆坏后留下残渣。
    for token in sorted(_GENERIC_TOKENS, key=len, reverse=True):
        residue = residue.replace(token, "")
    residue = _collapse_spaces(residue)
    return not residue


def build_search_query(raw: str, *, today: date | None = None) -> tuple[str, str]:
    """把用户问句改写成检索词。

    返回 ``(query, note)``，``note`` 标出改写类型，供 trace 观测：

    - ``"generic_news"``：裸泛新闻问句，已归一成「今日要闻 + 日期」
    - ``"trimmed"``：只剥掉了问句壳
    - ``""``：原样使用（剥壳没有产生变化，或剥完太短）
    """

    text = _collapse_spaces(raw or "")
    if not text:
        return "", ""

    # 先剥壳再判类型：用户不会在开头就写「今日要闻」，
    # 「帮我查一下昨天的新闻」剥完才是「昨天的新闻」。
    trimmed = _strip_lead(text)
    trimmed = _collapse_spaces(_strip_tail(trimmed))

    if is_generic_news_query(trimmed or text):
        today = today or date.today()
        return _GENERIC_NEWS_TEMPLATE.format(
            year=today.year, month=today.month, day=today.day
        ), "generic_news"

    if len(trimmed) >= _MIN_TRIMMED_CHARS and trimmed != text:
        return trimmed[:_MAX_QUERY_CHARS], "trimmed"
    return text[:_MAX_QUERY_CHARS], ""