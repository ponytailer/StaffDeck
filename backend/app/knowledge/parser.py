from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile


SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".doc"}

#: 除 \t / \n / \r 之外的控制字符（\v、\f 已先统一成换行）。
#: NUL 单独处理：PostgreSQL 的 text 列不接受 0x00，flush 时会直接抛
#: "A string literal cannot contain NUL (0x00) characters"，整个入库任务因此失败。
_CONTROL_CHARS = re.compile(r"[\x01-\x08\x0e-\x1f\x7f]")
#: 图片在正文里的占位协议。入库时由 app.knowledge.assets 换成带签名的真实 URL，
#: 解析层不认识存储层，只负责「图出现在哪个段落之后」。
DOCX_IMAGE_PLACEHOLDER = "kasset://"


@dataclass
class ExtractedImage:
    """从原文档里抽出来的一张图。"""

    data: bytes
    mime: str = "image/png"
    alt: str = ""
    width: int | None = None
    height: int | None = None


@dataclass
class ParsedDocument:
    """解析结果：正文（markdown）+ 正文里引用到的图片。

    ``text`` 里图片以 ``![图注](kasset://0)`` 占位，``images[0]`` 对应第一张。
    """

    text: str
    file_type: str
    images: list[ExtractedImage] = field(default_factory=list)


class KnowledgeParseError(ValueError):
    pass


def sanitize_text(text: str) -> str:
    """清掉 NUL 与无意义的控制字符。

    扫描件、老旧 docx、伪装的二进制文件抽出来的正文里确实会夹 NUL；这类字符
    既没法显示也没法入库（PG 直接拒收），统一在解析出口清掉，避免下游各写入点
    各清一遍、漏一处就炸在 commit 上。
    """

    if not text:
        return ""
    if "\x00" in text:
        text = text.replace("\x00", "")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n")
    return _CONTROL_CHARS.sub("", text)


def extract_text(filename: str, content: bytes) -> tuple[str, str]:
    text, file_type = _extract_raw_text(filename, content)
    return sanitize_text(text), file_type


def _extract_raw_text(filename: str, content: bytes) -> tuple[str, str]:
    """兼容入口：只要正文（图片信息丢弃）。"""

    parsed = extract_document(filename, content)
    return parsed.text, parsed.file_type


def extract_document(filename: str, content: bytes) -> ParsedDocument:
    """解析原文件，返回正文 + 正文里的图片。

    目前只有 ``.docx`` 会抽图：把图单独存成资产，正文里留 markdown 引用，
    检索与回答仍走文字（图片只是答案的补充）。
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise KnowledgeParseError(f"暂不支持 {suffix or 'unknown'} 文件格式。")
    if suffix == ".doc":
        raise KnowledgeParseError("暂不支持旧版 .doc 二进制格式，请转换为 .docx 后上传。")
    if suffix in {".txt", ".md", ".markdown"}:
        return ParsedDocument(text=_decode_text(content), file_type=suffix.lstrip("."))
    if suffix in {".html", ".htm"}:
        return ParsedDocument(text=_extract_html(content), file_type="html")
    if suffix == ".pdf":
        return ParsedDocument(text=_extract_pdf(content), file_type="pdf")
    if suffix == ".docx":
        return _extract_docx(content)
    raise KnowledgeParseError(f"暂不支持 {suffix} 文件格式。")


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _extract_html(content: bytes) -> str:
    text = _decode_text(content)
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "html.parser")
        for item in soup(["script", "style", "noscript"]):
            item.decompose()
        return soup.get_text("\n")
    except Exception:
        parser = _HTMLTextExtractor()
        parser.feed(text)
        return parser.text


def _extract_pdf(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - dependency availability differs by env.
        raise KnowledgeParseError("缺少 pypdf，无法解析 PDF。") from exc
    reader = PdfReader(BytesIO(content))
    pages: list[str] = []
    for index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"## 第 {index + 1} 页\n\n{page_text}")
    if not pages:
        return ""
    return "# PDF 文档\n\n" + "\n\n".join(pages)


def _extract_docx(content: bytes) -> ParsedDocument:
    """抽取 .docx 正文（含图片）。

    三条路径依次尝试：
    1. python-docx（正常文档，保留标题层级与表格，**并抽出段落里的图片**）；
    2. 裸 zip 解析 `word/document.xml`（python-docx 装不上或文档结构异常时兜底，不出图）；
    3. 纯文本兜底 —— 后缀写成 `.docx`、内容其实是文本（把 .txt / 网页复制内容直接改名）
       太常见了，与其报「无法解析」，不如按文本读进来。
    """
    if not content:
        raise KnowledgeParseError("docx 文档内容为空。")
    try:
        return _extract_docx_with_python_docx(content)
    except Exception:  # noqa: BLE001 - 逐级降级，最后统一给可读报错
        pass
    try:
        return ParsedDocument(text=_extract_docx_with_zip(content), file_type="docx")
    except Exception as exc:  # noqa: BLE001
        plain = _decode_docx_as_plain_text(content)
        if plain is not None:
            return ParsedDocument(text=plain, file_type="docx")
        raise KnowledgeParseError(_docx_error_message(content)) from exc


def _extract_docx_with_python_docx(content: bytes) -> ParsedDocument:
    """按文档顺序遍历段落与表格，段落里的图片紧随其文字写入占位。

    旧的实现分两轮（先所有段落、再所有表格），图片信息完全丢失。这里改成走
    ``document.element.body`` 的直接子节点，段落/表格保持原始先后，图片才能落在
    「它所在的那个段落之后」——分块时自然和该段文字进同一个 chunk。
    """

    from docx import Document
    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    document = Document(BytesIO(content))
    rows: list[str] = []
    images: list[ExtractedImage] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                heading_level = _docx_heading_level(
                    paragraph.style.name if paragraph.style else ""
                )
                rows.append(f"{'#' * heading_level} {text}" if heading_level else text)
            for image in _docx_paragraph_images(child, document):
                index = len(images)
                images.append(image)
                label = image.alt or f"图片 {index + 1}"
                rows.append(f"![{label}]({DOCX_IMAGE_PLACEHOLDER}{index})")
        elif child.tag == qn("w:tbl"):
            rows.extend(_docx_table_rows(child, document, images))
    return ParsedDocument(text="\n".join(rows), file_type="docx", images=images)


def _docx_table_rows(table_element, document, images: list[ExtractedImage]) -> list[str]:
    """表格按行输出，**单元格里的图片也抽**。

    操作手册最常见的排版是「左文右图」两列表格（2026-11-13 线上实例：正文写着
    「参考图例：」，图全在表格里）。只取 ``cell.text`` 会让这类文档的图一张都
    抽不到——而且正文里连退化标记都没有，从数据上完全看不出来图丢了。

    刻意不遍历 ``table.rows``：合并单元格会被重复返回，同一张图会入库两次。
    直接走 ``w:tbl > w:tr > w:tc > w:p`` 只按 XML 实际结构来。
    """

    from docx.oxml.ns import qn
    from docx.text.paragraph import Paragraph

    rows: list[str] = []
    for tr in table_element.findall(qn("w:tr")):
        cells: list[str] = []
        for tc in tr.findall(qn("w:tc")):
            parts: list[str] = []
            for paragraph_element in tc.findall(qn("w:p")):
                text = Paragraph(paragraph_element, document).text.strip()
                if text:
                    parts.append(text)
                for image in _docx_paragraph_images(paragraph_element, document):
                    index = len(images)
                    images.append(image)
                    label = image.alt or f"图片 {index + 1}"
                    parts.append(f"![{label}]({DOCX_IMAGE_PLACEHOLDER}{index})")
            if parts:
                cells.append(" ".join(parts))
        if cells:
            rows.append(" | ".join(cells))
    return rows


def _docx_paragraph_images(paragraph_element, document) -> list[ExtractedImage]:
    """取一个段落里的图片（inline / 浮动 anchor 都算，按出现顺序）。"""

    from docx.oxml.ns import qn

    images: list[ExtractedImage] = []
    for blip in paragraph_element.findall(".//" + qn("a:blip")):
        rel_id = blip.get(qn("r:embed"))
        if not rel_id:
            continue
        part = document.part.related_parts.get(rel_id)
        blob = getattr(part, "blob", None) if part is not None else None
        if not blob:
            continue
        mime = str(getattr(part, "content_type", "") or "image/png").split(";")[0].strip()
        if not mime.startswith("image/"):
            continue  # 视频/嵌入对象不处理
        width, height = _docx_drawing_extent(blip)
        images.append(
            ExtractedImage(
                data=blob,
                mime=mime or "image/png",
                alt=_docx_drawing_alt(blip),
                width=width,
                height=height,
            )
        )
    return images


def _docx_drawing_ancestor(element):
    """向上找到包住这张图的 ``w:drawing``（extent / docPr 都在它的子树里）。"""

    from docx.oxml.ns import qn

    node = element
    for _ in range(8):
        node = node.getparent()
        if node is None:
            return None
        if node.tag == qn("w:drawing"):
            return node
    return None


def _docx_drawing_extent(blip) -> tuple[int | None, int | None]:
    """把 EMU 尺寸换算成像素（1px = 9525 EMU），拿不到就留空。"""

    from docx.oxml.ns import qn

    drawing = _docx_drawing_ancestor(blip)
    if drawing is None:
        return None, None
    extent = drawing.find(".//" + qn("wp:extent"))
    if extent is None:
        return None, None
    try:
        cx = int(extent.get("cx") or 0)
        cy = int(extent.get("cy") or 0)
    except (TypeError, ValueError):
        return None, None
    if cx <= 0 or cy <= 0:
        return None, None
    return max(1, round(cx / 9525)), max(1, round(cy / 9525))


def _docx_drawing_alt(blip) -> str:
    """图片替代文字：Word 的「可选文字」描述优先，其次对象名。"""

    from docx.oxml.ns import qn

    drawing = _docx_drawing_ancestor(blip)
    if drawing is None:
        return ""
    doc_pr = drawing.find(".//" + qn("wp:docPr"))
    if doc_pr is None:
        return ""
    for key in ("descr", "title", "name"):
        value = str(doc_pr.get(key) or "").strip()
        if value:
            return value[:120]
    return ""


def _decode_docx_as_plain_text(content: bytes) -> str | None:
    """把「其实是纯文本」的 .docx 当文本读；不像文本则返回 None。

    用 NUL 字节先排掉二进制（ZIP / PDF / 旧版 .doc 里一定会有），
    再看可打印字符占比，避免把乱码当成正文喂进知识库。
    """
    sample = content[:4096]
    if b"\x00" in sample:
        return None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_text(text):
            return text
    return None


def _looks_like_text(text: str) -> bool:
    sample = text[:4096]
    if not sample.strip():
        return False
    printable = sum(1 for char in sample if char.isprintable() or char in "\r\n\t")
    return printable / len(sample) >= 0.95


def _docx_error_message(content: bytes) -> str:
    """按真实文件头给出可执行的报错，而不是笼统的「无法解析」。"""
    head = content[:8]
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "这个文件其实是旧版 .doc 格式，请用 Word 另存为 .docx 后重新上传。"
    if head.startswith(b"%PDF"):
        return "这个文件其实是 PDF，请把后缀改回 .pdf 后重新上传。"
    if head.startswith(b"PK"):
        return "docx 文件已损坏（缺少 word/document.xml），请用 Word 另存为 .docx 后重新上传。"
    return (
        "无法解析该文件：它不是有效的 .docx（OOXML）包，也不像纯文本。"
        "请用 Word 另存为 .docx，或直接上传 .txt / .md。"
    )


def _docx_heading_level(style_name: str) -> int | None:
    match = re.match(r"^(?:Heading|标题)\s*([1-6])$", style_name.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_docx_with_zip(content: bytes) -> str:
    """不装 python-docx 时的兜底：直接读 `word/document.xml` 的文本节点。

    失败时把原始异常抛给 `_extract_docx`，由它决定是否降级成纯文本、以及给什么报错。
    """
    with ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    parser = _DocxTextExtractor()
    parser.feed(xml)
    return parser.text


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data)


class _DocxTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(part.strip() for part in self._parts if part.strip())

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._parts.append(data)
