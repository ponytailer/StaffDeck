"""docx 抽图 → 资产落盘 → markdown 引用 → 检索载荷 的端到端契约。

设计取向：图片不做理解，只当答案的补充。入库时把图单独存成资产，正文里用标准
markdown 引用，检索仍走文字；回答时把图片 markdown 原样带回。
"""

from __future__ import annotations

import base64
from io import BytesIO

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api.knowledge_assets import read_knowledge_asset
from app.knowledge import assets as asset_module
from app.knowledge.assets import (
    asset_public_path,
    asset_signature,
    materialize_image_placeholders,
    save_asset,
    verify_asset_signature,
)
from app.knowledge.parser import extract_document
from app.knowledge.search_payload import compact_knowledge_search_payload

TENANT = "tenant_docx_img"
#: 1x1 透明 PNG，够 python-docx 写出一个真实的 image part
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _session(tmp_path, monkeypatch) -> Session:
    monkeypatch.setattr(asset_module.paths, "user_data_dir", lambda: tmp_path)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def _docx_bytes(*, paragraphs: int = 2) -> bytes:
    from docx import Document
    from docx.shared import Inches

    document = Document()
    document.add_heading("行李寄存", level=1)
    document.add_paragraph("住客可在 L 层行李服务处免费寄存。")
    document.add_picture(BytesIO(PNG_1PX), width=Inches(1))
    if paragraphs > 1:
        document.add_paragraph("非住客可使用水世界入口右侧付费寄存柜。")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_paragraph_image_becomes_markdown_placeholder() -> None:
    parsed = extract_document("寄存说明.docx", _docx_bytes())

    assert parsed.file_type == "docx"
    assert len(parsed.images) == 1
    assert parsed.images[0].data.startswith(b"\x89PNG")
    assert parsed.images[0].width and parsed.images[0].height

    lines = parsed.text.splitlines()
    placeholder_index = next(
        index for index, line in enumerate(lines) if "kasset://0" in line
    )
    heading_index = lines.index("# 行李寄存")
    trailing_index = lines.index("非住客可使用水世界入口右侧付费寄存柜。")
    # 图片落在它所在段落之后、下一段之前（分块时能与该段文字同 chunk）
    assert heading_index < placeholder_index < trailing_index


def test_docx_without_images_returns_no_assets() -> None:
    from docx import Document

    document = Document()
    document.add_paragraph("纯文字段落。")
    buffer = BytesIO()
    document.save(buffer)

    parsed = extract_document("纯文字.docx", buffer.getvalue())

    assert parsed.images == []
    assert parsed.text.strip() == "纯文字段落。"


def test_save_asset_deduplicates_by_content(tmp_path, monkeypatch) -> None:
    db = _session(tmp_path, monkeypatch)

    first = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=PNG_1PX, mime="image/png"
    )
    db.commit()
    second = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_2", data=PNG_1PX, mime="image/png"
    )
    db.commit()

    assert first.id == second.id
    stored = list((tmp_path / "knowledge_assets" / TENANT).rglob("*.png"))
    assert len(stored) == 1


def test_placeholder_becomes_asset_id_reference(tmp_path, monkeypatch) -> None:
    """chunk 里只留资产 id，不写带签名的长 URL。

    写 URL 有两条硬伤：约 66 字符会被 900 字切块切断；回答阶段还要模型复述，
    抄错 sig/id 都是静默裂图。
    """

    db = _session(tmp_path, monkeypatch)
    asset = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=PNG_1PX, mime="image/png"
    )
    db.commit()

    text = materialize_image_placeholders(
        "住客可在 L 层免费寄存。\n![行李寄存位置](kasset://0)", {0: asset}
    )

    assert text == f"住客可在 L 层免费寄存。\n![行李寄存位置](kasset://{asset.id})"
    assert "/api/knowledge/assets/" not in text
    assert "?sig=" not in text
    # 签名能力本身仍然可用，只是不在 chunk 里出现
    assert verify_asset_signature(asset.id, asset_signature(asset.id))
    assert not verify_asset_signature(asset.id, "deadbeefdeadbeef")


def test_missing_asset_degrades_to_plain_text(tmp_path, monkeypatch) -> None:
    text = materialize_image_placeholders("正文\n![图](kasset://3)", {})

    assert text == "正文\n[图片 4]"


def test_compacted_payload_uses_short_ref_and_legend() -> None:
    """正文被裁到 200 字时，图片引用仍要留在证据里，且换成 {{img:N}} + 图例。

    模型只复述 5 个字符的标记，真实 URL 放在 image_refs 里由后端落地时拼。
    """

    payload = {
        "chunks": [],
        "evidence_pack": [
            {
                "chunk_id": "kchunk_1",
                "document_id": "kdoc_1",
                "source_path": "行李服务/寄存",
                "section_path": "行李服务",
                "summary": "行李寄存说明",
                "content": "住客可在 L 层免费寄存。" * 120
                + "\n![行李寄存位置](kasset://kasset_ab12)",
            }
        ],
        "selected_documents": [],
        "selected_buckets": [],
        "selected_concepts": [],
        "okf_citations": [],
        "route_trace": [],
    }

    compacted = compact_knowledge_search_payload(payload, max_chars=1_200)
    content = compacted["evidence_pack"][0]["content"]
    legend = compacted["image_refs"]

    # 正文确实被裁剪，但图片标记被补回来了
    assert len(content) < len("住客可在 L 层免费寄存。" * 120)
    assert content.endswith("{{img:1}}")
    assert "kasset://" not in content
    # 图例：标记 → 图题 → 真实 URL → 来源
    assert legend == [
        {
            "ref": "{{img:1}}",
            "caption": "行李寄存位置",
            "url": asset_public_path("kasset_ab12"),
            "source": "行李服务/寄存",
        }
    ]


def test_image_numbering_is_stable_across_budget_shrinks() -> None:
    """同一张图在不同预算轮次里必须同号，否则模型引用的图会指向另一张。"""

    payload = {
        "chunks": [],
        "evidence_pack": [
            {
                "chunk_id": "kchunk_1",
                "content": "长正文" * 200 + "\n![甲](kasset://kasset_a)",
            },
            {"chunk_id": "kchunk_2", "content": "短\n![乙](kasset://kasset_b)"},
        ],
    }

    for budget in (12_000, 2_400, 900):
        compacted = compact_knowledge_search_payload(payload, max_chars=budget)
        refs = [item["ref"] for item in compacted.get("image_refs") or []]
        assert refs == ["{{img:1}}", "{{img:2}}"], budget
        first = compacted["evidence_pack"][0].get("content") or ""
        if "{{img:1}}" in first:
            assert "{{img:2}}" not in first



def test_section_nodes_keep_image_with_its_paragraph() -> None:
    """图片行必须和它上面的文字同属一个章节，否则检索命中文字时图不在证据里。"""

    from app.knowledge.service import _build_section_nodes, _normalize_text

    text = (
        "# 行李寄存\n\n"
        "住客可在 L 层行李服务处免费寄存。\n\n"
        "![行李寄存位置](kasset://kasset_ab12)\n\n"
        "非住客可使用水世界入口右侧付费寄存柜。"
    )
    nodes = _build_section_nodes(_normalize_text(text))
    section = next(node for node in nodes if node["title"] == "行李寄存")

    assert "住客可在 L 层行李服务处免费寄存。" in section["content"]
    assert "kasset_ab12" in section["content"]


def test_ingest_keeps_asset_placeholder_in_chunks_and_search_evidence(
    tmp_path, monkeypatch
) -> None:
    """走完整入库：原文档配图变成资产 + 正文留 kasset 占位，chunk 与证据里都带得到。"""

    import json

    from sqlmodel import select

    from app.db.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeIngestJob, Tenant
    from app.knowledge.schema import KnowledgeSearchRequest
    from app.knowledge.service import IngestPayload, KnowledgeService

    db = _session(tmp_path, monkeypatch)
    db.add(Tenant(id=TENANT, name="Docx Img"))
    db.add(KnowledgeBase(id="kb_img", tenant_id=TENANT, name="带图知识库"))
    db.commit()

    service = KnowledgeService(db)
    job = service.create_ingest_job(
        IngestPayload(
            tenant_id=TENANT,
            knowledge_base_id="kb_img",
            filename="寄存说明.docx",
            content_base64=base64.b64encode(_docx_bytes()).decode("ascii"),
        )
    )
    service._run_ingest_job(job.id)  # noqa: SLF001 - 同步跑完入库

    job = db.get(KnowledgeIngestJob, job.id)
    assert job is not None and job.status == "succeeded", job.error
    document = db.get(KnowledgeDocument, job.document_id)
    assert document is not None
    assert document.metadata_json["asset_count"] == 1
    # 入库不单独存 raw_text，正文在 section_tree 里；知识库预览也走这里
    section_text = "\n".join(
        str(node.get("content") or "")
        for node in document.metadata_json.get("section_tree") or []
    )
    assert "/api/knowledge/assets/" not in section_text
    assert "kasset://kasset_" in section_text

    chunks = db.exec(
        select(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id)
    ).all()
    assert chunks
    assert any("kasset://kasset_" in chunk.content for chunk in chunks)
    assert not any("/api/knowledge/assets/" in chunk.content for chunk in chunks)

    response = service.search(
        KnowledgeSearchRequest(
            tenant_id=TENANT,
            knowledge_base_ids=["kb_img"],
            query="行李寄存",
            mode="debug",
            need_evidence_pack=True,
        )
    )
    payload = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    assert "kasset://kasset_" in payload
    # 检索结果里也不该出现带签名的 URL（只在落地时拼）
    assert "?sig=" not in payload


def test_asset_endpoint_requires_valid_signature(tmp_path, monkeypatch) -> None:
    db = _session(tmp_path, monkeypatch)
    asset = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=PNG_1PX, mime="image/png"
    )
    db.commit()

    response = read_knowledge_asset(asset.id, asset_signature(asset.id), None, db)
    assert response.media_type == "image/png"

    with pytest.raises(HTTPException) as wrong_signature:
        read_knowledge_asset(asset.id, "deadbeefdeadbeef", None, db)
    assert wrong_signature.value.status_code == 403

    with pytest.raises(HTTPException) as missing:
        read_knowledge_asset("kasset_missing", None, None, db)
    assert missing.value.status_code == 404


# ---------------------------------------------------------------------------
# 回答侧：{{img:N}} 跨分片落地
# ---------------------------------------------------------------------------

from app.knowledge.image_refs import (  # noqa: E402
    ImageRefExpander,
    expand_image_refs,
    strip_image_refs,
)

LEGEND = [
    {
        "ref": "{{img:1}}",
        "caption": "行李寄存位置",
        "url": "/api/knowledge/assets/kasset_ab12?sig=deadbeef",
        "source": "行李服务/寄存",
    }
]
EXPANDED = "![行李寄存位置](/api/knowledge/assets/kasset_ab12?sig=deadbeef)"


@pytest.mark.parametrize(
    "parts",
    [
        ["结论：见{{", "img", ":1}}"],
        ["结论：见{{im", "g:1}}"],
        ["结论：见{{", "img:1}}"],
        ["{{img:1}}"],
        ["结论：见{{img:1}}", "，再看下一段"],
    ],
)
def test_expander_rebuilds_marker_split_across_chunks(parts) -> None:
    """SSE 分片会把标记切开，任何切法都不能把 {{img 漏给用户。"""

    expander = ImageRefExpander(LEGEND)
    out = "".join(expander.feed(part) for part in parts) + expander.flush()

    assert EXPANDED in out
    assert "{{img" not in out


def test_expander_drops_out_of_range_ref() -> None:
    """模型编了个不存在的编号：直接丢掉，别渲染出裂图。"""

    assert expand_image_refs("见{{img:9}}", LEGEND) == "见"


def test_expander_leaves_code_fence_untouched() -> None:
    text = "示例：\n```\n{{img:1}}\n```\n"
    assert expand_image_refs(text, LEGEND) == text


def test_strip_image_refs_cleans_markers_without_legend() -> None:
    """拿不到图例的降级路径：宁可少一张图，也不能漏内部标记。"""

    assert strip_image_refs("见{{img:1}}与{{img:2}}") == "见与"


# ---------------------------------------------------------------------------
# 安全：SVG 一律降级成下载
# ---------------------------------------------------------------------------


def test_svg_asset_is_served_as_attachment(tmp_path, monkeypatch) -> None:
    """同源渲染 SVG 里的脚本能读 localStorage 的登录态，必须堵掉。"""

    db = _session(tmp_path, monkeypatch)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    asset = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=svg, mime="image/svg+xml"
    )
    db.commit()

    response = read_knowledge_asset(asset.id, asset_signature(asset.id), None, db)

    assert response.media_type == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_png_asset_still_renders_inline(tmp_path, monkeypatch) -> None:
    db = _session(tmp_path, monkeypatch)
    asset = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=PNG_1PX, mime="image/png"
    )
    db.commit()

    response = read_knowledge_asset(asset.id, asset_signature(asset.id), None, db)

    assert response.media_type == "image/png"
    assert "content-disposition" not in response.headers


# ---------------------------------------------------------------------------
# 回收：图被别的库引用时不能删
# ---------------------------------------------------------------------------


def test_purge_keeps_asset_still_referenced_by_another_base(tmp_path, monkeypatch) -> None:
    from sqlmodel import select

    from app.db.models import KnowledgeChunk
    from app.knowledge.assets import asset_file_path, purge_knowledge_base_assets

    db = _session(tmp_path, monkeypatch)
    asset = save_asset(
        db, tenant_id=TENANT, knowledge_base_id="kb_1", data=PNG_1PX, mime="image/png"
    )
    db.add(
        KnowledgeChunk(
            tenant_id=TENANT,
            knowledge_base_id="kb_2",
            document_id="kdoc_2",
            bucket_id="kbucket_2",
            chunk_index=0,
            content=f"另一库仍在引用![图](kasset://{asset.id})",
        )
    )
    db.commit()
    file_path = asset_file_path(asset)

    assert purge_knowledge_base_assets(db, tenant_id=TENANT, knowledge_base_id="kb_1") == 0
    assert file_path.is_file()

    chunk = db.exec(select(KnowledgeChunk)).first()
    assert chunk is not None
    db.delete(chunk)
    db.commit()

    assert purge_knowledge_base_assets(db, tenant_id=TENANT, knowledge_base_id="kb_1") == 1
    assert not file_path.exists()


def test_placeholder_caption_falls_back_to_readable_default() -> None:
    """Word 自动生成的 ``Picture 1`` 当图题没有信息量，换成可读兜底。"""

    from app.knowledge.image_refs import build_image_refs

    payload = {
        "evidence_pack": [
            {"content": "![Picture 1](kasset://kasset_a)\n\n![报销流程](kasset://kasset_b)"}
        ]
    }
    _, legend = build_image_refs(payload)

    assert [item["caption"] for item in legend] == ["原文档配图", "报销流程"]


def test_appendix_only_when_model_used_no_image() -> None:
    """兜底补图最多 2 张，且只拼图不拼空 URL。"""

    from app.knowledge.image_refs import build_image_appendix

    legend = [
        {"ref": "{{img:1}}", "caption": "甲", "url": "/api/a.png?sig=1"},
        {"ref": "{{img:2}}", "caption": "乙", "url": "/api/b.png?sig=2"},
        {"ref": "{{img:3}}", "caption": "丙", "url": "/api/c.png?sig=3"},
    ]

    appendix = build_image_appendix(legend, 2)

    assert "**原文档配图**" in appendix
    assert "/api/a.png" in appendix and "/api/b.png" in appendix
    assert "/api/c.png" not in appendix
    assert build_image_appendix([{"ref": "{{img:1}}", "url": ""}]) == ""
