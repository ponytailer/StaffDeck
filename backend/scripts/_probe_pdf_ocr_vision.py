"""OCR 可行性验证：造扫描件风格 PDF → pymupdf 渲染 PNG → 现有 LLMClient 视觉 OCR。

运行：uv run --with pymupdf python scripts/_probe_pdf_ocr_vision.py
只读：不写业务表；模型配置直接按 id 读取。
"""
import base64
import sys
from pathlib import Path

import fitz  # pymupdf

from app.db import engine
from app.db.models import ModelConfig
from app.llm.client import LLMClient

PDF_PATH = Path("/tmp/_ocr_scan_test.pdf")
PNG_PATH = Path("/tmp/_ocr_scan_test.png")

OCR_PROMPT = """你是 OCR 引擎。请把图片中的所有文字内容完整转录出来。
要求：
- 按阅读顺序输出纯文本，保留段落换行。
- 表格用 | 分隔单元格。
- 不要添加任何解释、评论或总结，只输出转录内容。"""


def make_scan_style_pdf() -> None:
    """生成一页含中文文字的 PDF（模拟扫描件：无文字层不行，但这里文字层会在渲染后丢弃——直接把页转成图片再包回 PDF）。"""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    text = """月度运营报告（示例扫描件）

一、本月营收：人民币 1,284,500 元，环比增长 12.3%。
二、新增客户：47 家，其中企业客户 32 家，个人客户 15 家。
三、客户满意度：4.6 / 5.0，主要投诉集中在物流时效。
四、下月计划：
   1. 华东仓扩容至 8,000 平方米；
   2. 上线售后自动分单流程；
   3. 完成供应商年度评审（覆盖率目标 95%）。

报告编号：QR-2026-0915    密级：内部
"""
    page.insert_textbox(
        fitz.Rect(60, 60, 535, 780),
        text,
        fontsize=13,
        fontname="china-s",
    )
    # 关键一步：把页面渲染成图片，再用图片重建 PDF → 产物没有任何文字层，等价于扫描件
    pix = page.get_pixmap(dpi=150)
    pix.save(PNG_PATH)
    img_doc = fitz.open()
    img_page = img_doc.new_page(width=595, height=842)
    img_page.insert_image(fitz.Rect(0, 0, 595, 842), filename=str(PNG_PATH))
    img_doc.save(PDF_PATH)
    doc.close()
    img_doc.close()

    # 验证产物确实无文字层
    check = fitz.open(PDF_PATH)
    extracted = check[0].get_text().strip()
    print(f"[1] 生成模拟扫描件 {PDF_PATH}（{PDF_PATH.stat().st_size} bytes），pypdf 式提取结果: {extracted!r}")
    assert not extracted, "模拟扫描件不应有文字层"


def render_pages(dpi: int = 170) -> list[bytes]:
    doc = fitz.open(PDF_PATH)
    images: list[bytes] = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        images.append(pix.tobytes("png"))
    doc.close()
    print(f"[2] 渲染 {len(images)} 页 @ {dpi}dpi，PNG 大小: {[len(i) for i in images]}")
    return images


def main() -> None:
    make_scan_style_pdf()
    images = render_pages()

    with engine.connect() as conn:
        from sqlmodel import Session

    with Session(engine) as s:
        config = s.get(ModelConfig, "model_d749e31559f043c1")
        assert config is not None, "balanced 模型配置不存在"
        print(f"[3] OCR 模型: {config.model}（配置名 {config.name}）")

        image_part = {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(images[0]).decode("ascii"),
                "detail": "auto",
            },
        }
        payload = {
            "user_message": "请转录图片中的全部文字。",
            "conversation_context": {
                "messages": [
                    {"role": "user", "content": "请转录图片中的全部文字。", "images": [image_part]}
                ]
            },
        }
        client = LLMClient(config)
        import time

        started = time.time()
        result = client.generate_json(
            OCR_PROMPT,
            payload,
        )
        elapsed = time.time() - started
        text = result.get("text") if isinstance(result, dict) else str(result)
        print(f"[4] OCR 完成，耗时 {elapsed:.1f}s，结果类型 {type(result).__name__}")
        print("---- OCR 结果 ----")
        print(text if isinstance(text, str) else result)
        print("---- end ----")


if __name__ == "__main__":
    sys.exit(main())
