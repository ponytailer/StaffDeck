"""单轮对话耗时归因探针（只读，不写业务数据）。

把一个 turn「为什么慢」从猜测变成由 AgentEvent 还原的真实时间线。

用法：
    cd backend && env -u PYTHONPATH uv run python scripts/_probe_turn_latency.py <msg_前缀>
    # 可选参数
    #   --window 300   窗口秒数（默认 300）
    #   --sid  <session_id>   已知 session，配合 --at "2026-09-23 14:02:38" 用
    #   --at   "YYYY-MM-DD HH:MM:SS"   本地(GMT+8)起点
    #   --dump <event_type>    额外打印该类型事件的原始 payload（排查用）

关键约定（踩过的坑）：
  * LLM span 的 turn_id 是 harness turn_id（turn_<ms>_<rand>）；业务事件用 user_message_id（msg_…）。
    两类事件的正确连接键是 **client_turn_id**（`harness_action_created.turn_id` 里装的其实是 msg_…）。
  * DB 存 UTC，日志/界面是 GMT+8（本脚本统一按 GMT+8 打印）。
  * memory.capture 在回复产出之后才开始，不阻塞用户等待，**不计入模型等待**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select  # noqa: E402

from app.db.database import engine  # noqa: E402
from app.db.models import AgentEvent, Message  # noqa: E402

CST = timedelta(hours=8)
BLOCKING_AFTER_OPERATIONS = {"memory.capture"}  # 回复后才跑，不计入用户等待


def local(dt) -> str:
    return (dt + CST).strftime("%H:%M:%S.%f")[:-3] if dt else "-"


def secs(ms: Any) -> str:
    try:
        return f"{float(ms) / 1000:.2f}s"
    except (TypeError, ValueError):
        return "-"


def resolve_target(db: Session, args: argparse.Namespace) -> tuple[str, Any]:
    if args.sid and args.at:
        from datetime import datetime

        start = datetime.strptime(args.at, "%Y-%m-%d %H:%M:%S") - CST
        return args.sid, start
    msg = db.exec(select(Message).where(Message.id.startswith(args.target))).first()
    if msg is None:
        raise SystemExit(f"找不到以 {args.target} 开头的消息")
    print(f"目标消息 {msg.id}  role={msg.role}  session={msg.session_id}")
    return msg.session_id, msg.created_at


def load_events(db: Session, session_id: str, start, window: int) -> list[AgentEvent]:
    return list(
        db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.session_id == session_id,
                AgentEvent.created_at >= start - timedelta(seconds=5),
                AgentEvent.created_at <= start + timedelta(seconds=window),
            )
            .order_by(AgentEvent.created_at)
        ).all()
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="msg_ 前缀")
    ap.add_argument("--sid")
    ap.add_argument("--at")
    ap.add_argument("--window", type=int, default=300)
    ap.add_argument("--dump")
    args = ap.parse_args()
    if not (args.target or (args.sid and args.at)):
        raise SystemExit("需要 <msg_前缀>，或 --sid + --at")

    db = Session(engine)
    session_id, start = resolve_target(db, args)
    events = load_events(db, session_id, start, args.window)
    print(f"起点(本地)：{local(start)}   窗口 {args.window}s   事件 {len(events)} 条\n")

    # ---- 1. turn 边界 ----
    received = next((e for e in events if e.event_type == "user_message_received"), None)
    done = next(
        (e for e in reversed(events) if e.event_type in {"assistant_message_created", "complete"}),
        None,
    )
    if args.dump:
        for e in events:
            if e.event_type == args.dump:
                print(f"=== {e.event_type} {local(e.created_at)}")
                print(json.dumps(e.payload_json, ensure_ascii=False)[:3000])

    if received and done:
        wall = (done.created_at - received.created_at).total_seconds()
        print("── turn 边界 ──")
        print(f"  user_message_received  {local(received.created_at)}")
        print(f"  assistant_created      {local(done.created_at)}")
        print(f"  用户等待（墙钟）        {wall:.2f}s\n")

    # ---- 2. LLM 调用（按 client_turn_id 精确对齐）----
    client_turn_id = (received.payload_json.get("client_turn_id") if received else None) or ""
    finished = [
        e
        for e in events
        if e.event_type == "llm_call_finished" and e.payload_json.get("client_turn_id") == client_turn_id
    ]
    started = [
        e
        for e in events
        if e.event_type == "llm_call_started" and e.payload_json.get("client_turn_id") == client_turn_id
    ]
    print(f"── 本 turn 模型调用（client_turn_id={client_turn_id or '未取到'}）──")
    print(f"  started={len(started)}  finished={len(finished)}")
    if len(started) != len(finished):
        print("  ⚠️ 数量不一致（重试/短路各写一次事件属正常，按较小长度对齐）")
    blocking_ms = 0.0
    for e in finished:
        p = e.payload_json
        op = str(p.get("operation") or "")
        dur = float(p.get("duration_ms") or 0)
        is_blocking = op not in BLOCKING_AFTER_OPERATIONS
        if is_blocking:
            blocking_ms += dur
        input_tokens = p.get("input_tokens") or 0
        output_tokens = p.get("output_tokens") or 0
        ttft = p.get("ttft_ms") or 0
        # 非流式请求（stream=false）记不到真正的首包时间：ttft 会被填成整个 duration，
        # 此时拆不出 decode 阶段，直接标 "-"，避免打印出天文数字的 tok/s。
        decode_ms = dur - float(ttft)
        if not output_tokens or decode_ms < dur * 0.1:
            decode = "-"
        else:
            decode = f"{output_tokens / (decode_ms / 1000):.1f}"
        print(
            f"  {local(e.created_at)} {op:<28} {secs(dur):>8} "
            f"in={input_tokens:>6} out={output_tokens:>5} "
            f"思维链={p.get('reasoning_chars') or 0:>5}字 答案={p.get('output_chars') or 0:>5}字 "
            f"decode={decode:>6} payload={p.get('payload_chars')} "
            f"json={p.get('json_attempt')}/{p.get('json_max_attempts')}"
            f"{'' if is_blocking else '  (回复后,不计等待)'}"
        )
    print(f"\n  模型等待合计（阻塞部分） {secs(blocking_ms)}")
    if received and done:
        wall = (done.created_at - received.created_at).total_seconds()
        print(f"  非模型开销               {wall - blocking_ms / 1000:.2f}s\n")

    # ---- 3. 循环还原 ----
    actions = [e for e in events if e.event_type == "harness_action_created"]
    if actions:
        print("── AgentLoop 轮次（决策 → 工具）──")
        for e in actions:
            p = e.payload_json
            print(
                f"  {local(e.created_at)} iter={p.get('iteration')} "
                f"action={p.get('action')} tool={p.get('tool_name')}"
            )
        print()

    # ---- 4. 工具返回体积（判断是否被截断/需分页读）----
    tools = [e for e in events if e.event_type == "harness_tool_completed"]
    if tools:
        print("── 工具返回 ──")
        for e in tools:
            p = e.payload_json
            result = p.get("result") or {}
            data = result.get("data") if isinstance(result, dict) else None
            size = ""
            if isinstance(data, dict):
                for key in ("size", "chunk_count", "truncated", "kind", "match_count"):
                    if key in data:
                        size += f" {key}={data[key]}"
            print(
                f"  {local(e.created_at)} {p.get('tool_name')} "
                f"success={p.get('success')} err={p.get('error')}{size}"
            )
        print()

    # ---- 5. 窗口内其它模型调用（不属本 turn：检索链路 / 上一轮后台任务）----
    others = [
        e
        for e in events
        if e.event_type == "llm_call_finished"
        and e.payload_json.get("client_turn_id") != client_turn_id
    ]
    if others:
        print("── 窗口内其它模型调用（检索链路 / 其它 turn，仅参考）──")
        for e in others:
            p = e.payload_json
            print(
                f"  {local(e.created_at)} {str(p.get('operation')):<28} "
                f"{secs(p.get('duration_ms')):>8} ttft={secs(p.get('ttft_ms')):>7} "
                f"in={p.get('input_tokens')} out={p.get('output_tokens')} "
                f"payload={p.get('payload_chars')}"
            )


if __name__ == "__main__":
    main()
