"""自定义评审规则文件（ocr ``rule.json``）的结构校验与示例。

ocr 的规则解析是**四层优先级链**（见 open-codereview.ai/docs/review-rules）：
``--rule`` CLI 参数 > ``<repo>/.opencodereview/rule.json`` > ``~/.opencodereview/rule.json``
> 系统内置规则。我们存租户级一份，执行时写进临时目录并走最高优先级的 ``--rule`` 注入。

结构（三个字段互相独立）：
- ``include``：可选 glob 列表，命中的文件**绕过**内置默认排除（测试文件等）——是绕过不是白名单；
- ``exclude``：可选 glob 列表，命中即不评审（用户过滤器里优先级最高）；
- ``rules``：``{path, rule, merge_system_rule?}`` 数组，按声明顺序取第一个匹配 ``path``
  的条目作为该文件的评审提示；默认**替换**内置语言规则，``merge_system_rule=true``
  时与内置规则合并（内置规则按文件扩展名逐文件解析，因此一条 catch-all 也能各语言各得其所）。

Glob 用 doublestar 语义：``**`` 跨目录、``*`` 不跨 ``/``、支持 ``{a,b}`` 展开与
``[abc]``/``?``；匹配大小写不敏感。排查用 ``ocr rules check <path>``。
"""

from __future__ import annotations

import json
from typing import Any

MAX_ENTRIES = 100
MAX_PATTERN_LENGTH = 300
MAX_RULE_LENGTH = 4000
MAX_LIST_ENTRIES = 100


class RuleFileError(ValueError):
    """规则文件内容非法，message 可直接给前端。"""


def parse_rule_file_config(raw: str | dict[str, Any]) -> dict[str, Any]:
    """把用户提交的规则文件内容解析并校验成干净的结构（可 JSON 序列化落库）。

    接受 JSON 字符串或已解析的 dict；任何结构问题抛 ``RuleFileError``（message 中文、可直接展示）。
    """
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise RuleFileError(f"规则文件不是合法 JSON：{exc}") from exc
    elif isinstance(raw, dict):
        payload = raw
    else:
        raise RuleFileError("规则文件必须是 JSON 对象")

    if not isinstance(payload, dict):
        raise RuleFileError("规则文件顶层必须是 JSON 对象")

    config: dict[str, Any] = {}

    include = payload.get("include", [])
    if include is None:
        include = []
    if not isinstance(include, list) or len(include) > MAX_LIST_ENTRIES:
        raise RuleFileError(f"include 必须是不超过 {MAX_LIST_ENTRIES} 条的 glob 数组")
    for pattern in include:
        _check_pattern(pattern, "include")
    config["include"] = [str(pattern).strip() for pattern in include if str(pattern).strip()]

    exclude = payload.get("exclude", [])
    if exclude is None:
        exclude = []
    if not isinstance(exclude, list) or len(exclude) > MAX_LIST_ENTRIES:
        raise RuleFileError(f"exclude 必须是不超过 {MAX_LIST_ENTRIES} 条的 glob 数组")
    for pattern in exclude:
        _check_pattern(pattern, "exclude")
    config["exclude"] = [str(pattern).strip() for pattern in exclude if str(pattern).strip()]

    rules = payload.get("rules", [])
    if rules is None:
        rules = []
    if not isinstance(rules, list):
        raise RuleFileError("rules 必须是数组")
    if len(rules) > MAX_ENTRIES:
        raise RuleFileError(f"rules 条数不能超过 {MAX_ENTRIES}")

    parsed_rules: list[dict[str, Any]] = []
    for index, entry in enumerate(rules):
        if not isinstance(entry, dict):
            raise RuleFileError(f"rules[{index}] 必须是 {{path, rule}} 对象")
        path = str(entry.get("path") or "").strip()
        rule = str(entry.get("rule") or "").strip()
        if not path:
            raise RuleFileError(f"rules[{index}] 缺少 path（文件 glob 模式）")
        if len(path) > MAX_PATTERN_LENGTH:
            raise RuleFileError(f"rules[{index}] 的 path 超过 {MAX_PATTERN_LENGTH} 字符")
        if not rule:
            raise RuleFileError(f"rules[{index}] 缺少 rule（评审规则文本）")
        if len(rule) > MAX_RULE_LENGTH:
            raise RuleFileError(f"rules[{index}] 的 rule 超过 {MAX_RULE_LENGTH} 字符")
        merge = bool(entry.get("merge_system_rule") or False)
        parsed_rules.append({"path": path, "rule": rule, "merge_system_rule": merge})
    config["rules"] = parsed_rules

    if not (config["include"] or config["exclude"] or config["rules"]):
        raise RuleFileError("规则文件至少要有 include / exclude / rules 之一，否则留空删除这份规则")
    return config


def _check_pattern(pattern: Any, field: str) -> None:
    if not isinstance(pattern, str) or not pattern.strip():
        raise RuleFileError(f"{field} 里的每一条都必须是非空 glob 字符串")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise RuleFileError(f"{field} 里的模式超过 {MAX_PATTERN_LENGTH} 字符")


def sample_rule_file() -> dict[str, Any]:
    """前端「填入示例」用的样例：一条 catch-all 安全规则 + 排除生成代码。"""
    return {
        "include": [],
        "exclude": ["**/*.gen.ts", "**/generated/**", "**/vendor/**"],
        "rules": [
            {
                "path": "src/api/**/*.go",
                "rule": "所有导出的 handler 必须在处理前校验请求体；事务开始后必须立即 defer tx.Rollback()。",
            },
            {
                "path": "**/*mapper*.xml",
                "rule": "检查 SQL 注入风险、参数绑定遗漏与未闭合的 XML 标签。",
            },
            {
                "path": "**/*",
                "rule": "安全审查：标记硬编码密钥、未校验的重定向与缺失的权限校验。",
                "merge_system_rule": True,
            },
        ],
    }
