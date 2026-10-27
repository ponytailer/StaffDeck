"""扫描件 PDF 入库 OCR 兜底的单元测试。

覆盖三件事：
1. 门槛判定（只有 PDF + 文字层不足才触发，正常 PDF/docx 绝不触发）；
2. OCR 回包 → markdown 的拼接（markdown 优先、text 兜底、空页跳过、页数上限标注）；
3. 失败路径必须抛 KnowledgeParseError 且带可读原因（不能静默产出空文档）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.core.artifacts.ocr as ocr_client
from app.core.artifacts.ocr import OcrError
from app.knowledge import ocr_fallback
from app.knowledge.ocr_fallback import (
    _pages_to_markdown,
    extract_pdf_via_ocr,
    should_attempt_ocr,
)
from app.knowledge.parser import KnowledgeParseError


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把 OCR 相关配置钉死，避免受 .env 影响。"""

    fake = SimpleNamespace(
        knowledge_ocr_fallback_enabled=True,
        knowledge_ocr_min_text_chars=80,
        knowledge_ocr_max_pages=300,
        indra_ocr_base_url="http://ocr.local:8000",
        indra_ocr_api_key="",
        indra_ocr_timeout_seconds=300.0,
        indra_ocr_max_files=10,
        indra_ocr_max_file_mb=50,
    )
    monkeypatch.setattr(ocr_fallback, "get_settings", lambda: fake)
    return fake


def test_scanned_pdf_without_text_triggers_ocr(settings: SimpleNamespace) -> None:
    assert should_attempt_ocr("GB 29179-2012 消防应急救援 作业规程.pdf", "") is True


def test_pdf_with_only_a_few_chars_triggers_ocr(settings: SimpleNamespace) -> None:
    # 扫描件常带页码/水印，只看「是否为空」会漏判
    assert should_attempt_ocr("scan.pdf", "第 1 页") is True


def test_text_pdf_does_not_trigger_ocr(settings: SimpleNamespace) -> None:
    assert should_attempt_ocr("normal.pdf", "正文" * 200) is False


def test_non_pdf_never_triggers_ocr(settings: SimpleNamespace) -> None:
    assert should_attempt_ocr("scan.docx", "") is False
    assert should_attempt_ocr("scan.txt", "") is False


def test_disabled_by_config(settings: SimpleNamespace) -> None:
    settings.knowledge_ocr_fallback_enabled = False
    assert should_attempt_ocr("scan.pdf", "") is False


def test_pages_to_markdown_prefers_markdown_then_text() -> None:
    markdown, used = _pages_to_markdown(
        [
            {"index": 0, "markdown": "# 第一章\n内容 A"},
            {"index": 1, "markdown": "", "text": "纯文本页"},
            {"index": 2, "markdown": ""},
        ]
    )
    assert used == 2
    assert "第 1 页" in markdown and "# 第一章" in markdown
    assert "第 2 页" in markdown and "纯文本页" in markdown
    assert "第 3 页" not in markdown


def test_pages_to_markdown_empty_returns_zero() -> None:
    markdown, used = _pages_to_markdown([{"index": 0, "markdown": ""}])
    assert markdown == "" and used == 0


def test_extract_pdf_via_ocr_builds_markdown(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run_ocr(files, options):  # noqa: ANN001
        captured["files"] = files
        captured["options"] = options
        return {
            "model": "indra-ocr",
            "results": [
                {
                    "filename": "scan.pdf",
                    "n_pages": 2,
                    "error": None,
                    "pages": [
                        {"index": 0, "markdown": "# 4 作业规程\n步骤一"},
                        {"index": 1, "markdown": "步骤二"},
                    ],
                }
            ],
        }

    monkeypatch.setattr(ocr_client, "run_ocr", fake_run_ocr)
    text = extract_pdf_via_ocr("scan.pdf", b"%PDF-1.7 fake")

    assert "OCR 识别" in text
    assert "步骤一" in text and "步骤二" in text
    files = captured["files"]
    assert isinstance(files, list) and files[0][0] == "scan.pdf"
    assert files[0][2] == "application/pdf"


def test_extract_pdf_via_ocr_annotates_page_cap(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.knowledge_ocr_max_pages = 1
    monkeypatch.setattr(
        ocr_client,
        "run_ocr",
        lambda files, options: {
            "results": [
                {"n_pages": 50, "error": None, "pages": [{"index": 0, "markdown": "首页"}]}
            ]
        },
    )
    text = extract_pdf_via_ocr("big.pdf", b"%PDF-1.7 fake")
    assert "仅识别并入库前 1 页" in text


def test_extract_pdf_via_ocr_raises_when_service_unconfigured(
    settings: SimpleNamespace,
) -> None:
    settings.indra_ocr_base_url = ""
    with pytest.raises(KnowledgeParseError) as excinfo:
        extract_pdf_via_ocr("scan.pdf", b"%PDF-1.7 fake")
    assert "INDRA_OCR_BASE_URL" in str(excinfo.value)


def test_extract_pdf_via_ocr_translates_ocr_error(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(files, options):  # noqa: ANN001
        raise OcrError("连接被拒绝", kind="network")

    monkeypatch.setattr(ocr_client, "run_ocr", boom)
    with pytest.raises(KnowledgeParseError) as excinfo:
        extract_pdf_via_ocr("scan.pdf", b"%PDF-1.7 fake")
    message = str(excinfo.value)
    assert "OCR 服务连接失败" in message and "连接被拒绝" in message


def test_extract_pdf_via_ocr_raises_when_nothing_recognized(
    settings: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ocr_client,
        "run_ocr",
        lambda files, options: {
            "results": [{"error": "encrypted", "pages": [{"index": 0, "markdown": ""}]}]
        },
    )
    with pytest.raises(KnowledgeParseError) as excinfo:
        extract_pdf_via_ocr("scan.pdf", b"%PDF-1.7 fake")
    assert "encrypted" in str(excinfo.value)