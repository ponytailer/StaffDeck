"""问句改写的单元测试（不联网）。

每条断言都对应一个**真实故障模式**，不是补覆盖率：

1. 「今天的新闻有哪些」原样送引擎 → bing 返回日历网 / 老黄历 / 历史上的今天，
   模型据此回答「今天是 2026 年第 299 天，万圣节还有 2 天」（线上实测）。
2. 带实词的 query **不能**补日期：实测「新能源汽车补贴政策 2026年10月30日」
   被切成「新（汉语汉字）_百度百科」「新浪网」「汉典」，而原样检索能拿到
   中国政府网的政策原文。
3. 单独一个「什么」「现在」不能判成泛新闻问句，否则用户问「什么」也会去搜新闻。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.web_search.query import build_search_query, is_generic_news_query


@pytest.fixture(name="fixed_today")
def _fixed_today() -> date:
    return date(2026, 10, 30)


@pytest.mark.parametrize(
    "raw",
    ["今天的新闻有哪些", "今天有什么新闻", "今日要闻", "最近有什么大新闻",
     "现在有什么热点", "新闻", "今天有没有什么值得关注的新闻"],
)
def test_generic_news_query_normalised_to_dated_headline(raw: str, fixed_today: date) -> None:
    """裸泛新闻问句必须归一成「今日要闻 + 当天日期」。

    不加日期时 bing 返回站点首页（今日头条/央视网首页）；加日期后才是
    新闻正文页（光明日报要闻版、每日 60 秒新闻早报、新华社）。
    """

    query, note = build_search_query(raw, today=fixed_today)

    assert note == "generic_news"
    assert query == "今日要闻 2026年10月30日"


@pytest.mark.parametrize(
    "raw",
    [
        "新能源汽车补贴政策",
        "2026年新能源汽车最新补贴政策",
        "打印机怎么用",
        "报价单模板在哪下载",
        # 这两个抵消后仍有残留（\u201c有没有\u201d已剥，剩\u201c值得关注的\u201d已剥，见下一条断言说明）
    ],
)
def test_topic_query_never_gets_date_appended(raw: str, fixed_today: date) -> None:
    """带主题词的 query 绝不能补日期 —— 补了会把 query 切碎成单字条目。"""

    query, note = build_search_query(raw, today=fixed_today)

    assert note != "generic_news"
    assert "2026年10月30日" not in query


def test_topic_query_with_date_suffix_is_broken_proof(fixed_today: date) -> None:
    """反证：手工补日期确实会毁掉检索（这是规则存在的原因，不是假设）。"""

    query, note = build_search_query("新能源汽车补贴政策", today=fixed_today)

    assert query == "新能源汽车补贴政策"
    assert note == ""


@pytest.mark.parametrize("raw", ["什么", "现在", "怎么", "吗"])
def test_bare_filler_words_are_not_news_queries(raw: str) -> None:
    """只靠「虚词抵消成空」判定是不够的：必须有真实新闻锚点词。"""

    assert is_generic_news_query(raw) is False
    query, note = build_search_query(raw, today=date(2026, 10, 30))
    assert note != "generic_news"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("打印机驱动在哪里下载？", "打印机驱动在哪里下载"),
        ("请问一下，公司年假怎么算？", "公司年假怎么算"),
        ("差旅报销流程怎么走", "差旅报销流程"),
        ("现在的报销标准是多少", "现在的报销标准"),
        ("上海明天天气怎么样", "上海明天天气"),
    ],
)
def test_question_shell_stripped(raw: str, expected: str) -> None:
    """剥掉问句壳：搜索引擎不解析句式，虚词只会稀释关键词权重。"""

    query, note = build_search_query(raw)

    assert query == expected
    assert note == "trimmed"


def test_already_clean_query_kept_verbatim() -> None:
    query, note = build_search_query("打印机怎么用")

    assert query == "打印机怎么用"
    # 「怎么用」不在尾巴词表里（它是实义片段），所以原样使用并标空 note
    assert note == ""


def test_empty_query_returns_empty() -> None:
    assert build_search_query("") == ("", "")
    assert build_search_query("   ") == ("", "")


def test_long_query_truncated() -> None:
    query, _ = build_search_query("打印机" * 40)

    assert len(query) <= 60