"""仅知识库员工确定性问答工作流（harness_v2 直答路径）测试。

覆盖：
- `_is_knowledge_workflow_frame` 的准入条件（conversation/answer_only/
  无斜杠命令/无附件/无 general_skill+tool/有 knowledge_search 授权）；
- `_run_knowledge_workflow` 的三条路径：寒暄直回（0 检索）、代码直调检索
  后一次作答、检索失败/证据不可用降级返回 None。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.core.harness_v2_engine import (
    HarnessV2Engine,
    _is_knowledge_workflow_frame,
)
from app.core.task_request_compiler import CapabilityDescriptor, CapabilityManifest
from app.core.turn_planner import PlannedTaskFrame


def _descriptor(name: str, kind: str = "internal", *, available: bool = True) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"cap-{name}",
        name=name,
        kind=kind,  # type: ignore[arg-type]
        available=available,
    )


def _manifest(*descriptors: CapabilityDescriptor) -> CapabilityManifest:
    return CapabilityManifest(available=list(descriptors))


def _frame(*, kind: str = "conversation", decision: str = "answer_only") -> Any:
    return PlannedTaskFrame(
        kind=kind,  # type: ignore[arg-type]
        decision=decision,  # type: ignore[arg-type]
        user_intent="身高体重有要求吗",
    )


# ---------------------------------------------------------------------------
# 准入条件
# ---------------------------------------------------------------------------


def test_qualifies_for_pure_knowledge_conversation() -> None:
    assert _is_knowledge_workflow_frame(
        _frame(),
        _manifest(_descriptor("knowledge_search", "knowledge")),
        slash_command=None,
        has_attachments=False,
    )


def test_rejected_for_non_conversation_or_non_answer_only() -> None:
    manifest = _manifest(_descriptor("knowledge_search", "knowledge"))
    assert not _is_knowledge_workflow_frame(
        _frame(kind="sop"), manifest, slash_command=None, has_attachments=False
    )
    assert not _is_knowledge_workflow_frame(
        _frame(decision="clarify"), manifest, slash_command=None, has_attachments=False
    )


def test_rejected_when_slash_command_or_attachment() -> None:
    manifest = _manifest(_descriptor("knowledge_search", "knowledge"))
    assert not _is_knowledge_workflow_frame(
        _frame(), manifest, slash_command=object(), has_attachments=False
    )
    assert not _is_knowledge_workflow_frame(
        _frame(), manifest, slash_command=None, has_attachments=True
    )


def test_rejected_when_skill_or_tool_present() -> None:
    for kind in ("general_skill", "tool"):
        manifest = _manifest(
            _descriptor("knowledge_search", "knowledge"),
            _descriptor("some_skill", kind),
        )
        assert not _is_knowledge_workflow_frame(
            _frame(), manifest, slash_command=None, has_attachments=False
        )
    # 不可用能力不阻断（同 knowledge_prefetch 口径）
    manifest = _manifest(
        _descriptor("knowledge_search", "knowledge"),
        _descriptor("dead_skill", "general_skill", available=False),
    )
    assert _is_knowledge_workflow_frame(
        _frame(), manifest, slash_command=None, has_attachments=False
    )


def test_rejected_without_knowledge_search_authorization() -> None:
    assert not _is_knowledge_workflow_frame(
        _frame(),
        _manifest(_descriptor("session_title")),
        slash_command=None,
        has_attachments=False,
    )


# ---------------------------------------------------------------------------
# 工作流执行
# ---------------------------------------------------------------------------


class _FakeResponseGenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def generate(self, *args: Any) -> str:
        self.calls.append(args)
        return "身高体重有要求 [1]。"

    def generate_stream(self, *args: Any):  # noqa: ANN201
        raise AssertionError("非流式轮次应走 generate()")


class _FakeInvoker:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.invocations.append((name, arguments))
        return self.result


def _engine(response_generator: _FakeResponseGenerator) -> HarnessV2Engine:
    engine = object.__new__(HarnessV2Engine)
    engine.db = None
    engine.turn_stream = None
    engine.owner = SimpleNamespace(
        _get_persona_prompt=lambda *a, **k: None,
        response_generator=response_generator,
    )
    return engine


def _ok_search_result(**overrides: Any) -> dict[str, Any]:
    base = {
        "success": True,
        "data": {"chunks": [{"id": "c1"}], "evidence_pack": [{"id": "e1"}]},
        "chunk_count": 1,
        "citations": [{"id": "kref_1"}],
        "route_phases": ["document_route_small_candidate_shortcut"],
    }
    base.update(overrides)
    return base


def test_workflow_retrieves_in_code_then_answers_once() -> None:
    rg = _FakeResponseGenerator()
    engine = _engine(rg)
    invoker = _FakeInvoker(_ok_search_result())
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine._run_knowledge_workflow(
        SimpleNamespace(tenant_id="t"),
        SimpleNamespace(agent_id="a"),
        SimpleNamespace(task_id="task-1", status="running"),
        _frame(),
        SimpleNamespace(source_user_message="身高体重有要求吗"),
        invoker,
        object(),
        [],
        None,
        lambda t, p: events.append((t, p)),
    )

    assert result is not None
    assert result.status == "completed"
    assert result.reply_fragment == "身高体重有要求 [1]。"
    assert result.citations == [{"id": "kref_1"}]
    # 代码直调一次检索，不等 LLM 决策
    assert invoker.invocations == [
        ("knowledge_search", {"query": "身高体重有要求吗", "max_chunks": 8})
    ]
    # 只发生一次作答 LLM
    assert len(rg.calls) == 1
    assert [t for t, _ in events] == [
        "harness_tool_completed",
        "knowledge_workflow_completed",
    ]
    # 路由 phase 仍以 harness_tool_completed 形式可见（诊断脚本口径）
    tool_event = dict(events[0][1])
    assert tool_event["result"]["route_phases"] == [
        "document_route_small_candidate_shortcut"
    ]


def test_workflow_smalltalk_skips_retrieval() -> None:
    rg = _FakeResponseGenerator()
    engine = _engine(rg)
    engine._answer_direct_reply = lambda *a, **k: "你好呀，有什么可以帮你？"  # type: ignore[method-assign]
    invoker = _FakeInvoker(_ok_search_result())
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine._run_knowledge_workflow(
        SimpleNamespace(tenant_id="t"),
        SimpleNamespace(agent_id="a"),
        SimpleNamespace(task_id="task-1", status="running"),
        _frame(),
        SimpleNamespace(source_user_message="你好哇"),
        invoker,
        object(),
        [],
        None,
        lambda t, p: events.append((t, p)),
    )

    assert result is not None
    assert result.reply_fragment == "你好呀，有什么可以帮你？"
    assert invoker.invocations == []  # 寒暄不检索
    assert rg.calls == []
    assert [t for t, _ in events] == ["knowledge_workflow_smalltalk"]


def test_workflow_falls_back_when_search_fails() -> None:
    rg = _FakeResponseGenerator()
    engine = _engine(rg)
    invoker = _FakeInvoker({"success": False, "error": {"code": "KNOWLEDGE_NOT_AVAILABLE"}})
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine._run_knowledge_workflow(
        SimpleNamespace(tenant_id="t"),
        SimpleNamespace(agent_id="a"),
        SimpleNamespace(task_id="task-1", status="running"),
        _frame(),
        SimpleNamespace(source_user_message="身高体重有要求吗"),
        invoker,
        object(),
        [],
        None,
        lambda t, p: events.append((t, p)),
    )

    assert result is None
    assert rg.calls == []
    assert [t for t, _ in events] == ["knowledge_workflow_search_failed"]


def test_workflow_falls_back_when_payload_unusable() -> None:
    """data 被大 payload 机制换成文件指针（无 chunks/evidence）→ 降级标准路径。"""

    rg = _FakeResponseGenerator()
    engine = _engine(rg)
    invoker = _FakeInvoker(_ok_search_result(data={"sandbox_path": "/tmp/x.json"}))
    events: list[tuple[str, dict[str, Any]]] = []

    result = engine._run_knowledge_workflow(
        SimpleNamespace(tenant_id="t"),
        SimpleNamespace(agent_id="a"),
        SimpleNamespace(task_id="task-1", status="running"),
        _frame(),
        SimpleNamespace(source_user_message="身高体重有要求吗"),
        invoker,
        object(),
        [],
        None,
        lambda t, p: events.append((t, p)),
    )

    assert result is None
    assert rg.calls == []
    assert [t for t, _ in events] == ["knowledge_workflow_payload_unusable"]