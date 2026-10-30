"""_bounded_capability_result 的小标量随行行为（场景 C2 依赖）+ I1 分级内联契约。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.core.engine import step_support
from app.core.engine.step_loop import _bounded_capability_result


def test_chunk_count_preserved_inline() -> None:
    result: dict[str, Any] = {
        "success": True,
        "data": {"chunks": [{"content": "a"}, {"content": "b"}]},
        "chunk_count": 2,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded["chunk_count"] == 2
    assert bounded["success"] is True


def test_oversized_chunks_tiered_not_truncated() -> None:
    """I1：超预算的知识结果先分级降档——无 evidence 时 chunks 自己是正文载体，
    前 3 条保留正文（裁到 1.5K），不再截成半截 JSON 的 preview。"""

    result: dict[str, Any] = {
        "success": True,
        "data": {"chunks": [{"content": "x" * 20_000}]},
        "chunk_count": 1,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded.get("truncated") is None
    chunks = bounded["data"]["chunks"]
    assert chunks[0]["content"] == "x" * 1_500
    assert bounded["chunk_count"] == 1


def test_evidence_top3_keep_content_rest_indexed() -> None:
    """I1：evidence 前 3 条保留正文（各 ≤1.5K），其余退化成定位索引；
    chunks 与 evidence 同源，正文一律从 evidence 读。"""

    evidence = [
        {"chunk_id": f"c{i}", "content": "y" * 2_000, "summary": f"摘要{i}"}
        for i in range(6)
    ]
    result: dict[str, Any] = {
        "success": True,
        "data": {"evidence": evidence, "chunks": list(evidence)},
        "chunk_count": 6,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    data = bounded["data"]
    kept = [item for item in data["evidence"] if "content" in item]
    assert [item["chunk_id"] for item in kept] == ["c0", "c1", "c2"]
    assert all(len(item["content"]) == 1_500 for item in kept)
    assert all("content" not in item for item in data["evidence"][3:])
    # chunks 与 evidence 同源 → 全部退化为定位索引
    assert all("content" not in item for item in data["chunks"])


def test_tiering_disabled_falls_back_to_truncate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回滚开关关闭时恢复旧的截断 preview 行为。"""

    monkeypatch.setattr(step_support, "KNOWLEDGE_RESULT_TIERED_INLINE", False)
    result: dict[str, Any] = {
        "success": True,
        "data": {"chunks": [{"content": "x" * 20_000}]},
        "chunk_count": 1,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded.get("truncated") is True
    assert bounded.get("chunk_count") == 1
    assert "data" not in bounded


def test_chunk_count_absent_for_other_tools() -> None:
    result: dict[str, Any] = {"success": True, "data": {"ok": 1}}
    bounded = _bounded_capability_result("query_balance", result)
    assert "chunk_count" not in bounded


def _realistic_payload(evidence_count: int = 8) -> dict[str, Any]:
    """按 ``search_payload`` 的真实投影结构造载荷。

    关键点：证据列表的键是 ``evidence_pack``。旧用例一律用 ``evidence``，与实现
    犯了同一个错，所以「分级降档其实没生效」这个缺陷一直测不出来。
    """

    body = (
        "# 电脑与外设故障\n\n- **打印机**：办公区打印机故障先看面板报错（缺纸/卡纸/"
        "缺粉按提示处理），驱动问题在内网软件中心重装「打印服务包」。\n"
    )
    chunk = {
        "document_id": "kdoc_1",
        "bucket_id": "kb_1",
        "summary": body[:300],
        "source_ref": "IT-常见故障排查手册.md / 电脑与外设故障",
        "metadata": {"section_path": "电脑与外设故障", "section_title": "电脑与外设故障"},
    }
    evidence = [
        {
            **chunk,
            "chunk_id": f"kchunk_{index}",
            "content": (body * 8)[:2_000],
            "confidence_reason": "命中关键词「打印机」「驱动」",
        }
        for index in range(evidence_count)
    ]
    return {
        "chunk_count": evidence_count,
        "chunks": [
            {**chunk, "chunk_id": item["chunk_id"], "chunk_index": index}
            for index, item in enumerate(evidence)
        ],
        "evidence_pack": evidence,
        "selected_documents": [
            {
                "id": f"kdoc_{index}",
                "title": f"IT 文档 {index}",
                "filename": f"IT-文档{index}.md",
                "outline": [f"第{line}章 章节标题" for line in range(12)],
            }
            for index in range(3)
        ],
        "selected_buckets": [
            {
                "id": f"kb_{index}",
                "title": "电脑与外设故障",
                "summary": body[:300],
                "metadata": {"section_paths": ["电脑与外设故障", "常见问答"]},
            }
            for index in range(8)
        ],
        "selected_concepts": [
            {"concept_id": f"kc_{index}", "title": f"概念 {index}", "content": body * 10}
            for index in range(5)
        ],
        "okf_citations": [
            {"concept_id": f"kc_{index}", "title": f"概念 {index}"} for index in range(8)
        ],
        "route_trace": [{"phase": "bucket_route", "message": "选桶阶段说明"}],
    }


def test_evidence_pack_payload_is_tiered_not_truncated() -> None:
    """真实键名（``evidence_pack``）的知识结果超 10K 时必须分级降档。

    反证旧行为：只有 ``evidence`` 能被识别时，这份载荷会原样超预算 → 退化成
    10K 的 ``preview``（半截 JSON），模型读不懂只能 ``read_file`` 盲读或换词重搜。
    """

    result: dict[str, Any] = {
        "success": True,
        "data": _realistic_payload(),
        "chunk_count": 8,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded.get("truncated") is None
    data = bounded["data"]
    assert len(data["evidence_pack"]) == 8
    # 预算内，且证据正文至少保住 top-N（正文是答问依据，不能被元数据挤掉）
    serialized = len(json.dumps(bounded, ensure_ascii=False, sort_keys=True, default=str))
    assert serialized <= step_support._KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS
    kept = [item for item in data["evidence_pack"] if item.get("content")]
    assert kept, "top-N 证据正文必须保留，否则模型只能逐条 read_knowledge_chunk"
    assert all(len(item["content"]) <= 1_500 for item in kept)


def test_evidence_pack_hard_squeeze_keeps_json_valid() -> None:
    """正文全删仍超预算时：削导航区块与条数，绝不吐半截 JSON。"""

    payload = _realistic_payload(evidence_count=20)
    payload["selected_concepts"] = [
        {"concept_id": f"kc_{index}", "title": f"概念 {index}", "content": "z" * 4_000}
        for index in range(12)
    ]
    result: dict[str, Any] = {
        "success": True,
        "data": payload,
        "chunk_count": 20,
    }
    bounded = _bounded_capability_result("knowledge_search", result)
    assert bounded.get("truncated") is None
    data = bounded["data"]
    serialized = len(json.dumps(bounded, ensure_ascii=False, sort_keys=True, default=str))
    assert serialized <= step_support._KNOWLEDGE_RESULT_TRANSCRIPT_MAX_CHARS
    # JSON 结构完整：顶层键可读，chunk_count 随行不丢
    assert bounded["chunk_count"] == 20
    assert isinstance(data.get("evidence_pack"), list)
    assert data["evidence_pack"], "证据条不可清空，否则等于没检索"
