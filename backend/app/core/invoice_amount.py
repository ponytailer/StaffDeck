"""电子发票 PDF 价税合计提取（报销流程助手）。

从上传的电子发票 PDF 里解析「价税合计（小写）¥xxx」金额，多个发票累加成报销总金额。
只做文本匹配：数电票/增值税电子普通发票的版式里「价税合计」行都带（小写）¥金额；
解析失败的文件在结果里标注原因，由前端提示，不阻塞流程。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pypdf import PdfReader

# 「（小写）」/「(小写)」后面跟可选货币符的金额；去空白后匹配以兼容版式空格
_AMOUNT_RE = re.compile(r"[（(]\s*小写\s*[）)]\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
# 兜底：价税合计关键字之后的第一串金额（有的发票没有「小写」字样）
_TOTAL_RE = re.compile(r"价税合计[^0-9]{0,30}?[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")


def _pdf_text(pdf_path: Path) -> str:
    """提取 PDF 全部文本（去空白拼接，规避版式里字与字之间的空格/换行）。"""
    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "".join(parts)


def extract_invoice_total_text(text: str) -> str | None:
    """从发票文本里解析价税合计金额字符串（两位小数）；解析失败返回 None。"""
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    for pattern in (_AMOUNT_RE, _TOTAL_RE):
        match = pattern.search(compact)
        if not match:
            continue
        raw = match.group(1).replace(",", "")
        try:
            return f"{Decimal(raw):.2f}"
        except InvalidOperation:
            continue
    return None


def extract_invoice_total(pdf_path: Path) -> str | None:
    """解析单张发票 PDF 的价税合计金额；解析失败返回 None。"""
    try:
        text = _pdf_text(pdf_path)
    except Exception:
        return None
    return extract_invoice_total_text(text)


def sum_invoice_amounts(paths: list[str]) -> dict[str, Any]:
    """逐张解析并累加。返回 {items, total}；items 里带每张的金额或错误原因。"""
    items: list[dict[str, str | None]] = []
    total = Decimal("0")
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "文件不存在"})
            continue
        amount = extract_invoice_total(path)
        if amount is None:
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "未能解析出价税合计（非电子发票版式或扫描件）"})
            continue
        try:
            total += Decimal(amount)
        except InvalidOperation:
            items.append({"path": raw, "filename": path.name, "amount": None, "error": "金额格式异常"})
            continue
        items.append({"path": raw, "filename": path.name, "amount": amount, "error": None})
    return {"items": items, "total": f"{total:.2f}"}
