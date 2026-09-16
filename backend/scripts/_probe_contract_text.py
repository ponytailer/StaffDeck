"""探针 4：session_d405c314 完整时间线——用户消息/助手消息/产物/工具调用。"""

from sqlmodel import Session, select

from app.db import engine
from app.db.models import AgentEvent, Message

SID = "session_d405c314bb0e9612"

with Session(engine) as db:
    msgs = db.exec(
        select(Message)
        .where(Message.tenant_id == "tenant_demo", Message.session_id == SID)
        .order_by(Message.created_at)
    ).all()
    for m in msgs:
        meta = m.metadata_json or {}
        arts = meta.get("harness_artifacts") or []
        atts = meta.get("attachments") or m.content[:0]
        extra = ""
        if isinstance(atts, list) and atts:
            names = [a.get("filename") or a.get("name") for a in atts if isinstance(a, dict)]
            extra += f" attachments={names}"
        if arts:
            sizes = [(a.get("path"), a.get("size")) for a in arts if isinstance(a, dict)]
            extra += f" artifacts={sizes}"
        content = (m.content or "").replace("\n", " ")[:60]
        print(f"{m.created_at} [{m.role}] {content}{extra}")

    print("\n---- extract / write 工具调用 ----")
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == "tenant_demo", AgentEvent.session_id == SID)
        .order_by(AgentEvent.created_at)
    ).all()
    for ev in events:
        if ev.event_type != "harness_tool_completed":
            continue
        pj = ev.payload_json or {}
        tool = pj.get("tool_name")
        if tool not in ("extract_document_text", "write_file", "read_file", "list_files"):
            continue
        result = (pj.get("result") or {})
        data = result.get("data") or {} if isinstance(result, dict) else {}
        brief = {k: data.get(k) for k in ("source_path", "extracted_text_path", "characters", "empty", "path", "size") if k in data}
        print(ev.created_at, tool, "task=", str(pj.get("task_frame_id"))[:20], brief)
