"""评审结果 → 平台评论区 markdown 的生成（回写 PR/MR 用）。

ocr 的 ``result_json`` 是逐条评论（path / 行区间 / 内容 / 现有代码 / 建议），
这里拼成一段可直接贴进 GitHub / GitLab 评论框的 markdown。
"""

from __future__ import annotations

from typing import Any

# GitHub 评论正文上限 65536 字符；留余量截断，避免 422
MAX_BODY_CHARS = 60000

_HEADER_TEMPLATE = "## 🤖 AI Reviewer 评审报告\n"


def build_review_report_markdown(
    task: dict[str, Any],
    comments: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> str:
    """把任务快照 + 行级评论拼成 markdown 汇总。

    结构：头部（标题/分支/概要统计）→ 逐条意见（文件·行号 · 内容 · 代码片段）→ 尾注。
    超长时截断并标注省略条数，保证能贴进评论区。
    """
    summary = summary or {}
    lines: list[str] = []
    title = task.get("mr_title") or f"PR/MR #{task.get('mr_number')}"
    lines.append(_HEADER_TEMPLATE.rstrip("\n"))
    lines.append(f"**评审对象**：#{task.get('mr_number')} {title}")
    source = task.get("source_branch") or ""
    target = task.get("target_branch") or ""
    if source or target:
        lines.append(f"**分支**：`{source}` → `{target}`")
    meta: list[str] = []
    if summary.get("model"):
        meta.append(f"模型 {summary['model']}")
    if summary.get("files_reviewed") is not None:
        meta.append(f"评审文件 {summary['files_reviewed']}")
    if comments:
        meta.append(f"意见 {len(comments)} 条")
    if meta:
        lines.append("**概要**：" + " · ".join(str(item) for item in meta))
    lines.append("")
    lines.append("---")
    lines.append("")

    for index, comment in enumerate(comments, start=1):
        path = comment.get("path") or "(未知文件)"
        start = comment.get("start_line")
        end = comment.get("end_line")
        if start is None and end is None:
            location = "整体"
        elif end in (None, start):
            location = f"L{start}"
        else:
            location = f"L{start}-L{end}"
        lines.append(f"### {index}. `{path}` · {location}")
        content = (comment.get("content") or "").strip()
        if content:
            lines.append("")
            lines.append(content)
        existing = (comment.get("existing_code") or "").strip()
        suggestion = (comment.get("suggestion_code") or "").strip()
        if existing:
            lines.append("")
            lines.append("**现有代码**：")
            lines.append("```")
            lines.append(existing)
            lines.append("```")
        if suggestion:
            lines.append("")
            lines.append("**建议修改**：")
            lines.append("```suggestion")
            lines.append(suggestion)
            lines.append("```")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append(
        "> 由 AI Reviewer 自动生成，意见仅供人工复核参考。"
    )
    body = "\n".join(lines).strip() + "\n"
    if len(body) > MAX_BODY_CHARS:
        truncated_note = f"\n\n> ⚠️ 内容过长，已截断（原始 {len(body)} 字符，仅展示前 {MAX_BODY_CHARS} 字符）。"
        body = body[:MAX_BODY_CHARS].rstrip() + truncated_note
    return body
