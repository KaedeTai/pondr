"""Periodic self-reflection — maintain knowledge map.

Default: every PONDR_KMAP_INTERVAL_S (default 6h). Can also run on-demand.
For each top-level topic in the KB, ask the LLM to enumerate what's known
vs unknown sub-questions. Unknowns auto-enqueue research subtasks.
"""
from __future__ import annotations
import asyncio
import json
import os
from .. import llm
from ..kb import sqlite as kb_sql, knowledge_gaps as kg_kb
from ..utils.log import event, logger

INTERVAL_S = int(os.getenv("PONDR_KMAP_INTERVAL_S", str(6 * 3600)))
MAX_TOPICS_PER_RUN = 6

# Recursion caps for auto-enqueued reflection sub-tasks. A topic accumulates
# one `[gap] ` prefix per generation (root → `[gap] X` → `[gap] [gap] X` …);
# we count those prefixes and refuse to enqueue past the cap. Same idea for
# `[backtest]`. Defaults: at most one generation of children. Override via
# PONDR_MAX_GAP_DEPTH / PONDR_MAX_BACKTEST_DEPTH.
MAX_GAP_DEPTH = int(os.getenv("PONDR_MAX_GAP_DEPTH", "1"))
MAX_BACKTEST_DEPTH = int(os.getenv("PONDR_MAX_BACKTEST_DEPTH", "1"))


def _gap_depth(topic: str) -> int:
    """How many `[gap] ` ancestor generations this topic represents.

    Root research topics return 0. A first-generation auto-enqueued gap
    (`[gap] foo`) returns 1, second generation returns 2, etc.
    """
    return (topic or "").count("[gap]")


def _backtest_depth(topic: str) -> int:
    """Count of `[backtest]` / `[gap]` ancestry markers on this topic.

    Either prefix indicates the task itself was auto-enqueued from another
    auto-enqueued task — we use the same count to gate further backtest
    enqueues so backtest results don't trigger more backtests.
    """
    t = topic or ""
    return t.count("[backtest]") + t.count("[gap]")

# Heuristic: knowledge gaps mentioning these terms are good candidates for an
# automated backtest task.
_BT_TRIGGERS = (
    "sharpe", "drawdown", "backtest", "back-test", "back test",
    "strategy", "策略", "回測", "ma cross", "moving average",
    "mean reversion", "breakout", "z-score", "z score", "momentum",
)
_BT_STRATS = {
    "ma": "ma_cross", "moving average": "ma_cross", "ma cross": "ma_cross",
    "mean reversion": "mean_reversion", "z-score": "mean_reversion",
    "z score": "mean_reversion",
    "breakout": "breakout", "momentum": "breakout",
}
_BT_SYMBOLS = {
    "btc": "BTCUSDT", "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT", "ethereum": "ETHUSDT",
}


def _maybe_backtest_task(topic: str, sub_question: str) -> str | None:
    """Return a [backtest] task title if the gap looks like a backtest ask."""
    blob = f"{topic} {sub_question}".lower()
    if not any(t in blob for t in _BT_TRIGGERS):
        return None
    strat = next((v for k, v in _BT_STRATS.items() if k in blob), "ma_cross")
    sym = next((v for k, v in _BT_SYMBOLS.items() if k in blob), "BTCUSDT")
    return (f"[backtest] run_backtest('{strat}','{sym}') for: "
            f"{sub_question[:120]}")

SYS_REFLECT = (
    "role: reflection agent. given a topic and a digest of "
    "what's been recorded about it (notes), enumerate concrete sub-questions "
    "that should be answerable about this topic. For each sub-question, mark "
    "whether you currently have a confident answer ('known'), are mid-way "
    "('researching'), or don't know ('unknown'). "
    "If you wanted to do something but couldn't because a capability/tool was "
    "missing, call report_capability_gap. "
    "Output STRICT JSON: {\"known\": [{\"q\": str, \"a\": str}], "
    "\"researching\": [str], \"unknown\": [str]} — keep arrays under 6 each."
)


async def reflect_on_topic(topic: str) -> dict:
    notes = await kb_sql.find_notes_by_topic(f"finding/{topic[:60]}", limit=10)
    notes_blob = "\n".join(f"- ({(n.get('confidence') or 0):.2f}) {n['content'][:300]}"
                           for n in notes) or "(no notes yet)"
    resp = await llm.chat([
        {"role": "system", "content": SYS_REFLECT},
        {"role": "user",
         "content": f"Topic: {topic}\n\nKB digest:\n{notes_blob}"},
    ], temperature=0.3, max_tokens=4096)
    txt = llm.assistant_text(resp)
    obj: dict = {"known": [], "researching": [], "unknown": []}
    try:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(txt[i:j + 1])
    except Exception as e:
        logger.warning(f"reflect_on_topic parse: {e}")

    counts = {"known": 0, "researching": 0, "unknown": 0, "enqueued": 0}
    gap_depth = _gap_depth(topic)
    bt_depth = _backtest_depth(topic)
    gap_capped = gap_depth >= MAX_GAP_DEPTH
    bt_capped = bt_depth >= MAX_BACKTEST_DEPTH
    if gap_capped:
        logger.info(
            f"reflect: skip [gap] enqueue, depth {gap_depth} >= "
            f"{MAX_GAP_DEPTH} (topic={topic[:80]!r})"
        )
    for k in (obj.get("known") or [])[:6]:
        if isinstance(k, dict) and k.get("q"):
            await kg_kb.upsert(topic, str(k["q"])[:200], status="known",
                               answer_summary=str(k.get("a", ""))[:500])
            counts["known"] += 1
    for s in (obj.get("researching") or [])[:6]:
        if isinstance(s, str):
            await kg_kb.upsert(topic, s[:200], status="researching")
            counts["researching"] += 1
    for s in (obj.get("unknown") or [])[:6]:
        if isinstance(s, str):
            gap_id = await kg_kb.upsert(topic, s[:200], status="unknown")
            # Enqueue research subtask for unknown — only if depth permits.
            if not gap_capped:
                try:
                    tid = await kb_sql.add_task(
                        f"[gap] {topic} :: {s[:120]}",
                        description=f"Knowledge gap auto-enqueued by reflector "
                                    f"(kg_id={gap_id})")
                    counts["enqueued"] += 1
                except Exception as e:
                    logger.warning(f"gap enqueue: {e}")
            # If the gap mentions a strategy/quant keyword, also queue a
            # concrete run_backtest task so the executor will pick it up.
            # Capped separately so a backtest result can't trigger another
            # backtest.
            if not bt_capped:
                try:
                    bt_task = _maybe_backtest_task(topic, s)
                    if bt_task:
                        await kb_sql.add_task(bt_task,
                            description=f"Auto-enqueued backtest from kg_id={gap_id}")
                        counts["enqueued"] += 1
                except Exception as e:
                    logger.warning(f"backtest auto-enqueue: {e}")
            counts["unknown"] += 1
    event("knowledge_map_topic", topic=topic, gap_depth=gap_depth,
          bt_depth=bt_depth, **counts)
    return counts


async def reflect_all() -> dict:
    """Reflect on top recent active topics."""
    tasks = await kb_sql.list_tasks(limit=80)
    seen_topics: list[str] = []
    for t in tasks:
        topic = (t.get("topic") or "").split(" — ")[0][:120]
        if topic and topic not in seen_topics:
            seen_topics.append(topic)
        if len(seen_topics) >= MAX_TOPICS_PER_RUN:
            break
    summary = {"topics": len(seen_topics), "enqueued": 0, "known": 0,
               "researching": 0, "unknown": 0}
    for topic in seen_topics:
        try:
            counts = await reflect_on_topic(topic)
            for k in ("enqueued", "known", "researching", "unknown"):
                summary[k] += counts[k]
        except Exception as e:
            logger.warning(f"reflect topic '{topic}': {e}")
    event("knowledge_map_run", **summary)
    return summary


async def run_periodic():
    logger.info(f"knowledge_map periodic loop (every {INTERVAL_S}s)")
    # Initial delay so feeds + first task settle before first reflection
    await asyncio.sleep(min(120, INTERVAL_S))
    while True:
        try:
            s = await reflect_all()
            logger.info(f"knowledge_map: {s}")
        except Exception as e:
            logger.warning(f"knowledge_map run: {e}")
        await asyncio.sleep(INTERVAL_S)
