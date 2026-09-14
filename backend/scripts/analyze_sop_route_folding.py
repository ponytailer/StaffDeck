"""SOP 路径折叠潜力分析（诊断脚本，只读）。

回答一个问题：**在当前 SOP 图里，"槽位已满"之后还有多少个节点是
必须停下来让 LLM / 外部系统决定的？**

模型：
- 把一次执行切成若干「段」（segment）。段的边界 = 必须停下的地方。
- 段内节点可以在一次外层迭代里被确定性推完（零 LLM、零中间落库）。
- 边界只有四类：
    1. slot_gap        需要用户输入
    2. conditional     条件边无法用槽位静态证明（依赖 LLM 或外部结果）
    3. external_result 强制能力/知识调用后的下一跳取决于返回值
    4. terminal        终点

统计两种口径：
- 现状（as-is）：当前 sop_step_executor 六场景能覆盖的比例
- 折叠后（folded）：纯流转段整体折叠，段数 = 必须停下的次数

用法：``.venv/bin/python scripts/analyze_sop_route_folding.py``
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import text

from app.db.database import engine

UNCONDITIONAL = {"", "default", "else"}


def _load_skills() -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select skill_id, version, name, status, content_json "
                "from skills order by skill_id"
            )
        ).fetchall()
    out: list[dict[str, Any]] = []
    for skill_id, version, name, status, content in rows:
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                continue
        if not isinstance(content, dict) or not content.get("nodes"):
            continue
        out.append(
            {
                "skill_id": skill_id,
                "version": version,
                "name": name,
                "status": status,
                "content": content,
            }
        )
    return out


def _outgoing(content: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for edge in content.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src = str(edge.get("source_node_id") or "")
        if src:
            out.setdefault(src, []).append(edge)
    return out


def classify(
    node: dict[str, Any],
    out_edges: list[dict[str, Any]],
    *,
    slots_satisfied: bool = True,
) -> tuple[bool, str]:
    """返回 (是否可被确定性推完, 原因标签)。

    ``slots_satisfied``：分析口径。True = 假设槽位已满（用户补完信息后的
    场景），此时节点自身可继续推进；False = 现状口径（缺槽即停）。
    """

    node_type = str(node.get("type") or "").strip()
    refs = node.get("capability_refs") or {}
    req_tools = list(refs.get("required_tool_ids") or [])
    req_gs = list(refs.get("required_general_skill_ids") or [])
    req_kb = list(refs.get("required_knowledge_base_ids") or [])
    slots = [str(s) for s in (node.get("expected_user_info") or []) if str(s).strip()]

    conditional = [
        e
        for e in out_edges
        if str(e.get("condition") or "").strip().lower() not in UNCONDITIONAL
    ]
    uncond = [
        e
        for e in out_edges
        if str(e.get("condition") or "").strip().lower() in UNCONDITIONAL
    ]

    if node_type == "handoff" and not out_edges:
        return True, "handoff_passthrough"  # 场景 F：零 LLM 发起转人工
    if not out_edges:
        return False, "terminal"
    if slots and not slots_satisfied:
        return False, "slot_gap"
    if conditional:
        return False, "conditional"
    if len(uncond) != 1:
        return False, "ambiguous_edges"
    if req_kb:
        # 场景 C：预检索动作确定，但检索结果决定后续 → 此段到此结束
        return False, "external_result(knowledge)"
    if req_tools or req_gs:
        # 场景 E：参数可由槽位直组，但返回值参与后续判定
        return False, "external_result(capability)"
    if node_type == "decision":
        return False, "decision_llm"
    if slots:
        return True, "slot_consume"  # 槽位已满：消费既有信息后纯流转
    return True, "pure_transition"


def walk(
    start: str,
    nodes: dict[str, dict[str, Any]],
    outgoing: dict[str, list[dict[str, Any]]],
    *,
    slots_satisfied: bool = True,
) -> tuple[int, str, list[str]]:
    """从 start 出发，返回 (确定性段长, 终止原因, 走过的节点)。"""

    node_id = start
    path: list[str] = []
    reason = "terminal"
    guard = 0
    while node_id and node_id in nodes and guard < 200:
        guard += 1
        node = nodes[node_id]
        foldable, why = classify(
            node, outgoing.get(node_id, []), slots_satisfied=slots_satisfied
        )
        path.append(node_id)
        reason = why
        if not foldable:
            break
        nexts = [
            str(e.get("next_node_id") or "")
            for e in outgoing.get(node_id, [])
            if str(e.get("condition") or "").strip().lower() in UNCONDITIONAL
        ]
        nexts = [n for n in nexts if n not in path]  # 防环
        node_id = nexts[0] if nexts else ""
        if not node_id:
            reason = "terminal"
    return len(path), reason, path


def analyze(content: dict[str, Any], *, slots_satisfied: bool = True) -> dict[str, Any]:
    nodes = {
        str(n.get("node_id") or ""): n
        for n in (content.get("nodes") or [])
        if isinstance(n, dict) and n.get("node_id")
    }
    outgoing = _outgoing(content)
    start = str(content.get("start_node_id") or "").strip()
    if not start or start not in nodes:
        start = next(iter(nodes), "")
    if not start:
        return {}

    # 从**每个**节点出发各走一次：SOP 多数是线性链，"槽位补满后从当前
    # 节点继续"才是真实场景，只看 start 会得到误导性的短段。
    lengths: list[int] = []
    reasons: Counter[str] = Counter()
    entry_reasons: Counter[str] = Counter()
    for node_id in nodes:
        length, reason, _ = walk(
            node_id, nodes, outgoing, slots_satisfied=slots_satisfied
        )
        lengths.append(length)
        reasons[reason] += 1
    entry_length, entry_reason, entry_path = walk(
        start, nodes, outgoing, slots_satisfied=slots_satisfied
    )
    entry_reasons[entry_reason] += 1

    total = len(nodes)
    avg = sum(lengths) / len(lengths) if lengths else 0.0
    longest = max(lengths, default=0)
    # 折叠后需要停下的次数 ≈ 每个"段起点"一次；对线性图约等于
    # 段数 = 节点数 / 平均段长
    segments = total / avg if avg else float(total)
    return {
        "total_nodes": total,
        "entry_length": entry_length,
        "avg_segment": avg,
        "longest_segment": longest,
        "segments": segments,
        "reasons": dict(reasons),
        "lengths": lengths,
    }


def condition_kind(text: str) -> str:
    """把边条件的自然语言粗略归类（启发式，用于估收益）。

    目的是回答：这条 condition 能不能被编译成**可求值谓词**？
    - unconditional  : 无条件边，本就确定性
    - slots_satisfied: 「字段齐了」类，可由槽位直接求值
    - slots_bool     : `a && b && c` 形式的字段布尔式，可求值
    - value_compare  : 数值/取值比较，左值常来自槽位，右值可能来自检索
    - external_result: 依赖能力/检索的返回值，必须停下等
    - user_intent    : 依赖用户确认意图（可归约为槽位）
    - llm_judge      : 真正的语义判断，必须 LLM
    """

    import re as _re

    t = " ".join(str(text or "").split()).strip().lower()
    if not t or t in UNCONDITIONAL:
        return "unconditional"
    if "satisfied" in t or "缺字段" in t or "字段" in t and "齐" in t:
        return "slots_satisfied"
    if any(k in t for k in (">", "<", "大于", "小于", "超过", "不足")):
        return "value_compare"
    if any(
        k in t
        for k in (
            "成功",
            "失败",
            "返回",
            "result",
            "obtained",
            "error",
            "调用",
            "检索完成",
            "提交",
        )
    ):
        return "external_result"
    if any(k in t for k in ("用户确认", "用户拒绝", "用户要求", "确认提交")):
        return "user_intent"
    if _re.fullmatch(r"[a-z0-9_\s&|()!]+", t) and ("&&" in t or "||" in t or "!" in t):
        return "slots_bool"
    if _re.fullmatch(r"[a-z0-9_\s&|()!]+", t):
        return "slots_bool"
    return "llm_judge"


def main() -> None:
    skills = _load_skills()
    print(f"扫描到 {len(skills)} 个含 SOP 图的技能\n")

    agg_reasons_sat: Counter[str] = Counter()
    agg_reasons_now: Counter[str] = Counter()
    all_lengths_sat: list[int] = []
    all_lengths_now: list[int] = []
    total_nodes = 0
    total_segments_sat = 0.0
    total_segments_now = 0.0
    longest_rows: list[tuple[int, str]] = []

    for skill in skills:
        sat = analyze(skill["content"], slots_satisfied=True)
        now = analyze(skill["content"], slots_satisfied=False)
        if not sat:
            continue
        total_nodes += sat["total_nodes"]
        total_segments_sat += sat["segments"]
        total_segments_now += now["segments"]
        for k, v in sat["reasons"].items():
            agg_reasons_sat[k] += v
        for k, v in now["reasons"].items():
            agg_reasons_now[k] += v
        all_lengths_sat.extend(sat["lengths"])
        all_lengths_now.extend(now["lengths"])
        longest_rows.append(
            (sat["longest_segment"], f"{skill['skill_id']} ({skill['name']})")
        )
        print(
            f"{skill['skill_id']:34s} {skill['name'][:14]:16s} "
            f"nodes={sat['total_nodes']:2d} "
            f"avg_seg={sat['avg_segment']:4.1f} "
            f"longest={sat['longest_segment']:2d} "
            f"entry={sat['entry_length']:2d}  "
            f"{sat['reasons']}"
        )

    print("\n=== 汇总（假设槽位已满 = 用户补完信息后）===")
    if total_nodes:
        avg = sum(all_lengths_sat) / len(all_lengths_sat)
        print(f"总节点数                  : {total_nodes}")
        print(f"平均确定性段长            : {avg:.2f} 节点")
        print(f"最长确定性段              : {max(all_lengths_sat)} 节点")
        print(f"段长分布                  : {dict(sorted(Counter(all_lengths_sat).items()))}")
        print(f"现状口径（缺槽即停）段长  : {dict(sorted(Counter(all_lengths_now).items()))}")
        print(
            f"\n当前实现：每节点一次外层迭代 ≈ {total_nodes} 次编译+落库\n"
            f"折叠后  ：每段一次外层迭代 ≈ {total_segments_sat:.0f} 次\n"
            f"框架开销折减比            : {total_nodes / max(total_segments_sat, 1):.1f}x"
        )

    print("\n=== 「停下来」的原因分布（槽位已满口径）===")
    for k, v in agg_reasons_sat.most_common():
        print(f"  {k:34s} {v}")
    print("\n=== 「停下来」的原因分布（现状口径）===")
    for k, v in agg_reasons_now.most_common():
        print(f"  {k:34s} {v}")

    print("\n=== 最长确定性段 Top10 ===")
    for length, label in sorted(longest_rows, reverse=True)[:10]:
        print(f"  {length:2d}  {label}")

    # ---- 条件边可编译性 ----
    print("\n=== 全部条件边（非无条件）的语义归类 ===")
    kinds: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    for skill in skills:
        for edge in skill["content"].get("edges") or []:
            if not isinstance(edge, dict):
                continue
            cond = str(edge.get("condition") or "")
            kind = condition_kind(cond)
            if kind == "unconditional":
                continue
            kinds[kind] += 1
            samples.setdefault(kind, [])
            if len(samples[kind]) < 3 and cond.strip():
                samples[kind].append(cond.strip()[:60])
    total_cond = sum(kinds.values())
    for kind, count in kinds.most_common():
        pct = count / total_cond if total_cond else 0
        print(f"  {kind:18s} {count:3d}  ({pct:.0%})")
        for s in samples.get(kind, []):
            print(f"       · {s}")

    foldable_ratio = (
        (kinds["slots_satisfied"] + kinds["slots_bool"] + kinds["user_intent"])
        / total_cond
        if total_cond
        else 0
    )
    print(
        f"\n  可编译为「槽位可求值谓词」的比例 ≈ {foldable_ratio:.0%}"
        f"（slots_satisfied + slots_bool + user_intent，共 {total_cond} 条条件边）"
    )


if __name__ == "__main__":
    main()
