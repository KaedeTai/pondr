"""Periodic curriculum generator.

Reads notes + backtest results + knowledge gaps and asks the LLM to organise
them into a textbook-style tree (chapters → sections → leaves), each leaf
annotated with a status badge and mastery_pct.

Diffs the new tree against the existing one; if anything materially changed
(promoted leaf, new chapter, mastery delta > 5pp), logs a one-line summary
event so the dashboard can surface "📚 promoted X.Y to medium" style logs.
"""
from __future__ import annotations
import asyncio
import json
import os
import time

import aiosqlite

from .. import config, llm
from ..kb import sqlite as kb_sql, curriculum as curr_kb
from ..utils.log import event, logger

INTERVAL_S = int(os.getenv("PONDR_CURR_INTERVAL_S", str(6 * 3600)))
INITIAL_DELAY_S = int(os.getenv("PONDR_CURR_INITIAL_DELAY_S", "90"))
MIN_NOTES_TO_GENERATE = int(os.getenv("PONDR_CURR_MIN_NOTES", "3"))


SYS_PROMPT = (
    "role: curriculum architect. organise knowledge into "
    "a textbook-style tree: top-level chapters → sections → optional sub-"
    "leaves. Each node MUST have:\n"
    "  - title (≤80 chars)\n"
    "  - description (≤200 chars)\n"
    "  - status: 'solid' | 'medium' | 'open'\n"
    "  - mastery_pct: int 0-100\n"
    "  - note_ids: array of int (subset of provided notes)\n"
    "  - related_backtest_ids: array of int (subset of provided backtests)\n"
    "  - related_gap_ids: array of int (subset of provided gaps)\n"
    "  - children: array of nodes (recursive)\n"
    "Rules:\n"
    "  • status='solid' iff ≥3 notes AND avg confidence ≥ 0.75\n"
    "  • status='medium' iff some notes (≥1) OR ≥1 supporting backtest\n"
    "  • status='open' iff only gaps / no notes\n"
    "  • mastery_pct should reflect both confidence and breadth of coverage\n"
    "  • Aim for 3-6 top-level chapters; total ≤ 30 leaves\n"
    "  • Chapters group related sections (e.g. '經典策略', '風險管理', "
    "'市場微結構', 'Crypto-specific')\n"
    "  • USE THE LANGUAGE FROM USER PREFERENCES for all titles/descriptions\n"
    "Output ONLY valid JSON: {\"tree\":[<root_nodes>]} — no surrounding prose."
)


def _diverse_recent_notes(rows: list[dict], k: int) -> list[dict]:
    """Sample notes by topic-prefix diversity then by recency.

    First pass: walk in DB-recency order keeping at most one row per topic
    prefix until we have k. Second pass (if still short): top up with the
    most recent remaining rows. Returns at most k items.
    """
    seen: set[str] = set()
    primary: list[dict] = []
    rest: list[dict] = []
    for n in rows:
        topic = (n.get("topic") or "")[:40]
        if topic and topic not in seen:
            seen.add(topic)
            primary.append(n)
        else:
            rest.append(n)
        if len(primary) >= k:
            break
    if len(primary) < k:
        primary.extend(rest[: k - len(primary)])
    return primary[:k]


async def _gather_inputs(max_notes: int = 10,
                        max_gaps: int = 10,
                        max_backtests: int = 5) -> dict:
    """Pull recent notes / gaps / backtests and produce a digest the LLM can
    read in <1.5k tokens (down from ~3k)."""
    # Pull a wider pool then pick the most diverse-by-topic subset.
    note_pool = await kb_sql.list_notes(limit=max(max_notes * 4, 30))
    notes = _diverse_recent_notes(note_pool, max_notes)
    gaps_rows: list[dict] = []
    bt_rows: list[dict] = []
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        # Prefer gaps that are still actionable (unknown / researching).
        cur = await db.execute(
            "SELECT id, topic, sub_question, status, answer_summary "
            "FROM knowledge_gaps "
            "WHERE status IN ('unknown', 'researching') "
            "ORDER BY id DESC LIMIT ?", (max_gaps,))
        gaps_rows = [dict(r) for r in await cur.fetchall()]
        if len(gaps_rows) < max_gaps:
            # Top up with whatever else exists so we don't return empty
            need = max_gaps - len(gaps_rows)
            seen_ids = {g["id"] for g in gaps_rows}
            cur = await db.execute(
                "SELECT id, topic, sub_question, status, answer_summary "
                "FROM knowledge_gaps ORDER BY id DESC LIMIT ?", (max_gaps * 2,))
            for r in await cur.fetchall():
                d = dict(r)
                if d["id"] in seen_ids:
                    continue
                gaps_rows.append(d)
                if len(gaps_rows) >= max_gaps:
                    break
        cur = await db.execute(
            "SELECT id, strategy, symbol, n_ticks, metrics_json, confidence "
            "FROM backtests ORDER BY id DESC LIMIT ?", (max_backtests,))
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            except Exception:
                d["metrics"] = {}
            bt_rows.append(d)
    return {
        "notes": [
            {"id": n["id"],
             "topic": (n.get("topic") or "")[:60],
             "content": (n.get("content") or "")[:140],
             "confidence": n.get("confidence")}
            for n in notes
        ],
        "knowledge_gaps": [
            {"id": g["id"], "topic": g.get("topic"),
             "q": g.get("sub_question"), "status": g.get("status"),
             "answer": (g.get("answer_summary") or "")[:160]}
            for g in gaps_rows
        ],
        "backtests": [
            {"id": b["id"], "strategy": b.get("strategy"),
             "symbol": b.get("symbol"), "n_ticks": b.get("n_ticks"),
             "sharpe": (b.get("metrics") or {}).get("sharpe"),
             "max_drawdown": (b.get("metrics") or {}).get("max_drawdown"),
             "confidence": b.get("confidence")}
            for b in bt_rows
        ],
    }


def _flatten(tree: list[dict]) -> list[dict]:
    out: list[dict] = []
    def go(n):
        out.append(n)
        for c in (n.get("children") or []):
            go(c)
    for r in tree:
        go(r)
    return out


def _validate_tree(tree: list[dict], notes: list[dict],
                  gaps: list[dict], backtests: list[dict]) -> list[dict]:
    """Sanity-check the LLM's output: clamp ints, drop invalid ids, default
    missing fields. Modifies in place but also returns the same list."""
    valid_note_ids = {n["id"] for n in notes}
    valid_gap_ids = {g["id"] for g in gaps}
    valid_bt_ids = {b["id"] for b in backtests}

    def _clean(node: dict) -> dict:
        node["title"] = (node.get("title") or "").strip()[:120]
        node["description"] = (node.get("description") or "").strip()[:400]
        s = (node.get("status") or "open").lower()
        node["status"] = s if s in ("solid", "medium", "open") else "open"
        try:
            node["mastery_pct"] = max(0.0, min(100.0,
                float(node.get("mastery_pct") or 0)))
        except Exception:
            node["mastery_pct"] = 0.0
        node["note_ids"] = [int(i) for i in (node.get("note_ids") or [])
                            if isinstance(i, (int, float, str))
                            and str(i).isdigit() and int(i) in valid_note_ids]
        node["related_gap_ids"] = [int(i) for i in (node.get("related_gap_ids") or [])
                                   if isinstance(i, (int, float, str))
                                   and str(i).isdigit() and int(i) in valid_gap_ids]
        node["related_backtest_ids"] = [int(i) for i in (node.get("related_backtest_ids") or [])
                                        if isinstance(i, (int, float, str))
                                        and str(i).isdigit() and int(i) in valid_bt_ids]
        # Confidence average over linked notes (best effort)
        if node["note_ids"]:
            note_confs = [n.get("confidence") for n in notes
                          if n["id"] in node["note_ids"]
                          and n.get("confidence") is not None]
            if note_confs:
                node["avg_confidence"] = sum(note_confs) / len(note_confs)
        node["children"] = [_clean(c) for c in (node.get("children") or [])
                            if isinstance(c, dict) and (c.get("title") or "").strip()]
        return node

    return [_clean(n) for n in tree if isinstance(n, dict)
            and (n.get("title") or "").strip()]


async def _llm_generate(inputs: dict) -> list[dict]:
    """One LLM call → tree. Returns [] on failure."""
    user_msg = (
        f"NOTES:\n"
        f"{json.dumps(inputs['notes'], ensure_ascii=False)[:1200]}\n\n"
        f"KNOWLEDGE GAPS:\n"
        f"{json.dumps(inputs['knowledge_gaps'], ensure_ascii=False)[:800]}\n\n"
        f"BACKTESTS:\n"
        f"{json.dumps(inputs['backtests'], ensure_ascii=False)[:600]}\n\n"
        f"Build the curriculum tree. Output JSON only."
    )
    # Curriculum strings are surfaced verbatim on the dashboard, so honor the
    # user's language preference even though this is technically an internal
    # generation step.
    try:
        from ..kb import preferences as prefs_kb
        user_lang = await prefs_kb.get_active_language()
    except Exception:
        user_lang = None
    resp = await llm.chat(
        [{"role": "system", "content": SYS_PROMPT},
         {"role": "user", "content": user_msg}],
        temperature=0.2, max_tokens=3500, language_hint=user_lang)
    txt = llm.assistant_text(resp).strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"):
            txt = txt[4:]
        txt = txt.strip()
    i = txt.find("{")
    if i < 0:
        logger.warning(f"curriculum LLM: no '{{' in output: {txt[:160]!r}")
        return []
    candidate = txt[i:]
    obj = None
    # Try strict parse first
    try:
        obj = json.loads(candidate)
    except Exception:
        pass
    # Fallback: chop trailing incomplete content and close brackets/braces.
    if obj is None:
        depth_braces = 0
        depth_brackets = 0
        in_str = False
        escape = False
        # Find last index where the structure is "safe" (just closed a value)
        last_safe = -1
        for k, ch in enumerate(candidate):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth_braces += 1
            elif ch == "}":
                depth_braces -= 1
                if depth_braces == 0 and depth_brackets == 0:
                    last_safe = k
            elif ch == "[":
                depth_brackets += 1
            elif ch == "]":
                depth_brackets -= 1
        if last_safe > 0:
            try:
                obj = json.loads(candidate[: last_safe + 1])
            except Exception:
                obj = None
        if obj is None:
            # Try: close all open brackets/braces with the right characters
            # to recover the partial tree.
            tail = ("]" * max(0, depth_brackets)) + ("}" * max(0, depth_braces))
            try:
                obj = json.loads(candidate + tail)
                logger.info(f"curriculum LLM: recovered truncated JSON "
                            f"(closed {depth_brackets} ] + {depth_braces} }})")
            except Exception as e:
                logger.warning(f"curriculum LLM: JSON parse failed: {e}; "
                               f"raw start={candidate[:120]!r}")
                return []
    return (obj or {}).get("tree") or []


def _diff_summary(prev_flat: list[dict], new_flat: list[dict]) -> dict:
    by_title_prev = {n["title"]: n for n in prev_flat}
    by_title_new = {n["title"]: n for n in new_flat}
    promoted = []
    demoted = []
    new_titles = []
    removed = []
    rank = {"open": 0, "medium": 1, "solid": 2}
    for t, n in by_title_new.items():
        if t not in by_title_prev:
            new_titles.append(t)
            continue
        p = by_title_prev[t]
        ps = (p.get("status") or "open").lower()
        ns = (n.get("status") or "open").lower()
        if rank.get(ns, 0) > rank.get(ps, 0):
            promoted.append(f"{t}: {ps}→{ns}")
        elif rank.get(ns, 0) < rank.get(ps, 0):
            demoted.append(f"{t}: {ps}→{ns}")
    for t in by_title_prev:
        if t not in by_title_new:
            removed.append(t)
    return {"promoted": promoted, "demoted": demoted,
            "new": new_titles, "removed": removed,
            "size_prev": len(prev_flat), "size_new": len(new_flat)}


async def regenerate(force: bool = False) -> dict:
    """One-shot regenerate. Returns a summary dict."""
    inputs = await _gather_inputs()
    if not force and len(inputs["notes"]) < MIN_NOTES_TO_GENERATE:
        return {"skipped": True,
                "reason": f"only {len(inputs['notes'])} notes "
                          f"(< {MIN_NOTES_TO_GENERATE})"}
    prev_flat = _flatten(await curr_kb.tree())
    tree = await _llm_generate(inputs)
    if not tree:
        return {"error": "LLM produced no tree"}
    tree = _validate_tree(tree, inputs["notes"],
                          inputs["knowledge_gaps"], inputs["backtests"])
    if not tree:
        return {"error": "tree empty after validation"}
    out = await curr_kb.replace_tree(tree)
    new_flat = _flatten(await curr_kb.tree())
    diff = _diff_summary(prev_flat, new_flat)
    overall = await curr_kb.overall_mastery()
    event("curriculum_regenerated",
          inserted=out["inserted"], overall_mastery=round(overall, 1),
          promoted=len(diff["promoted"]), demoted=len(diff["demoted"]),
          new=len(diff["new"]), removed=len(diff["removed"]))
    if diff["promoted"]:
        for line in diff["promoted"][:5]:
            logger.info(f"📚 promoted: {line}")
    if diff["new"]:
        logger.info(f"📚 added: {', '.join(diff['new'][:5])}")
    return {"inserted": out["inserted"], "overall_mastery": overall,
            "diff": diff}


async def run_periodic():
    logger.info(f"curriculum periodic loop (every {INTERVAL_S}s, "
                f"initial delay {INITIAL_DELAY_S}s)")
    await asyncio.sleep(INITIAL_DELAY_S)
    while True:
        try:
            s = await regenerate(force=False)
            logger.info(f"curriculum: {s}")
        except Exception as e:
            logger.warning(f"curriculum run: {e}")
        await asyncio.sleep(INTERVAL_S)
