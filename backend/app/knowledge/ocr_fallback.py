"""扫描件 PDF 入库兜底：文字层抽不出内容时改走 OCR。

**为什么需要**：``pypdf`` 的 ``extract_text()`` 只能读 PDF 的**文字层**。影印件、
扫描件（如 GB 标准、合同扫描、盖章文件）整页是图片，文字层为空 → 入库直接报
「文档没有可用文本内容」。这类文件在政企知识库里占比不低。

**设计取向**：

1. **只管 PDF**，且只在文字层判定为「无有效内容」时触发——正常文字 PDF 绝不走
   OCR（慢几十倍且会引入识别误差）。
2. **复用既有 Indra OCR 服务**（``app.core.artifacts.ocr``，Agent 广场「OCR 助手」
   同一客户端），不引入 tesseract/paddleocr 等新依赖与模型文件。
3. **失败不阻断**：OCR 不可达/超时/报错时原样抛出可读错误，让入库任务正常失败，
   而不是静默产出空文档。
4. **逐页拼接 markdown**：保留标题层级与表格结构（OCR 服务按版面重建），比纯
   text 更利于下游分块与检索。
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.knowledge.parser import KnowledgeParseError

# 注意：``app.core.artifacts.ocr`` 必须**延迟导入**。``app.core.__init__`` 会
# 拉起 facade → turn → invoke，而 invoke 又反向 import ``app.knowledge.service``；
# 本模块由 service 顶层 import，在此处顶层再导入 core 会形成循环导入。

logger = logging.getLogger(__name__)

#: 允许走 OCR 兜底的文件类型（目前只有 PDF：docx 是结构化文档，文字层不会缺失）。
OCR_FALLBACK_FILE_TYPES = {"pdf"}


def _file_type_of(filename: str) -> str:
    _, _, suffix = (filename or "").rpartition(".")
    return suffix.strip().lower()


def should_attempt_ocr(filename: str, text: str) -> bool:
    """判断是否该用 OCR 兜底。

    双重门槛：文件类型是 PDF，且文字层字符数低于 ``knowledge_ocr_min_text_chars``。
    门槛不只看「是否为空」——扫描件偶尔带几个页码/水印字符，只看空值会漏判。
    """

    settings = get_settings()
    if not settings.knowledge_ocr_fallback_enabled:
        return False
    if _file_type_of(filename) not in OCR_FALLBACK_FILE_TYPES:
        return False
    body = (text or "").strip()
    return len(body) < max(0, int(settings.knowledge_ocr_min_text_chars or 0))


def _pages_to_markdown(pages: list[dict[str, Any]]) -> tuple[str, int]:
    """把 OCR 回包的逐页结果拼成 markdown 正文，返回 (正文, 有效页数)。"""

    blocks: list[str] = []
    used = 0
    for position, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        page_markdown = str(page.get("markdown") or "").strip()
        if not page_markdown:
            # 上游未给 markdown 时退回 text：仍然可用，只是丢了版面结构
            page_markdown = str(page.get("text") or "").strip()
        if not page_markdown:
            continue
        page_index = page.get("index")
        number = page_index + 1 if isinstance(page_index, int) and page_index >= 0 else position + 1
        blocks.append(f"## 第 {number} 页\n\n{page_markdown}")
        used += 1
    if not blocks:
        return "", 0
    return "# PDF 文档（OCR 识别）\n\n" + "\n\n".join(blocks), used


def extract_pdf_via_ocr(filename: str, content: bytes) -> str:
    """调用 Indra OCR 识别整份 PDF，返回可直接入库的 markdown 正文。

    任何失败都抛 :class:`KnowledgeParseError`（带可读原因），供入库任务把失败
    详情写到 job.error 上，用户能看到「是 OCR 服务的问题」而不是笼统的解析失败。
    """

    settings = get_settings()
    base_url = (settings.indra_ocr_base_url or "").strip()
    if not base_url:
        raise KnowledgeParseError(
            "这个 PDF 没有文字层（可能是扫描件），需要 OCR 识别，"
            "但平台未配置 OCR 服务（INDRA_OCR_BASE_URL）。"
            "请联系管理员配置，或上传带文字层的 PDF。"
        )
    from app.core.artifacts.ocr import OcrError, OcrOptions, run_ocr

    max_pages = int(settings.knowledge_ocr_max_pages or 0)
    options = OcrOptions(
        format="json",
        max_pages=max_pages or None,
        image_ocr=True,
        include_lines=False,
        include_blocks=False,
        include_timings=False,
    )
    try:
        payload = run_ocr(
            [(filename or "document.pdf", content, "application/pdf")],
            options,
        )
    except OcrError as exc:
        hint = {
            "config": "OCR 服务未配置",
            "timeout": "OCR 识别超时",
            "http": "OCR 服务返回错误",
            "network": "OCR 服务连接失败",
            "decode": "OCR 回包无法解析",
            "payload": "OCR 请求参数不合法",
        }.get(exc.kind, "OCR 调用失败")
        raise KnowledgeParseError(
            f"这个 PDF 没有文字层（可能是扫描件），OCR 识别失败：{hint}（{exc}）。"
        ) from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        raise KnowledgeParseError("OCR 服务没有返回任何识别结果。")

    first = results[0] if isinstance(results[0], dict) else {}
    upstream_error = first.get("error")
    pages = first.get("pages") if isinstance(first.get("pages"), list) else []
    markdown, used_pages = _pages_to_markdown(pages)
    if not markdown:
        detail = f"（上游错误：{upstream_error}）" if upstream_error else ""
        raise KnowledgeParseError(f"OCR 未能识别出任何文字{detail}。")

    logger.info(
        "扫描件 OCR 完成 file=%s pages=%s/%s chars=%s",
        filename,
        used_pages,
        len(pages),
        len(markdown),
    )
    # 页数被上限截断时明确标注：否则用户会以为整本都入库了
    total_pages = first.get("n_pages")
    if isinstance(total_pages, int) and total_pages > used_pages and max_pages:
        markdown += (
            f"\n\n> 注：原文共 {total_pages} 页，超过 OCR 页数上限 {max_pages} 页，"
            f"仅识别并入库前 {used_pages} 页。"
        )
    return markdown