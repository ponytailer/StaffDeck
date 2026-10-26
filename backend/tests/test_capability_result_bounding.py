"""_bounded_capability_result 的小标量随行行为（场景 C2 依赖）+ I1 分级内联契约。"""

from __future__ import annotations

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
