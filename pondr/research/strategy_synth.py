"""End-to-end auto strategy synthesis flow.

Triggered when:
  (a) the synthesizer publishes a finding whose body matches strategy
      keywords (regex below) — we enqueue a ``[design_strategy] <hypothesis>``
      task.
  (b) the loop.py dispatcher sees that prefix and calls run() here.

This module owns the ``design → run → iterate`` mini-loop. We:

  1. Ask the LLM to write the on_tick code (design_strategy tool).
  2. Run the strategy on the most-traded symbol we have ticks for.
  3. If the first run produced something interesting (>0 trades, no compile
     error), ask the LLM to iterate once on whichever metric is weakest.
  4. Persist a finding summarising what was tried + the winner.

The whole thing intentionally short-circuits if the LLM produces broken
code — the strategy row is still saved (status=compile_error) so a human
or later iterate call can fix it.
"""
from __future__ import annotations
import re
import time

from .. import llm
from ..kb import sqlite as kb_sql, strategies as strat_kb
from ..tools.strategy import (design_strategy, run_strategy,
                              iterate_strategy)
from ..utils.log import event, logger


STRATEGY_TASK_PREFIX = "[design_strategy]"

# Keyword patterns that suggest a finding is talking about a strategy idea.
# Case-insensitive; both English and Traditional Chinese covered. We require
# at least one keyword AND a hypothesis-shaped phrase ("when X, then Y" /
# "predicts" / "leads to" / "信號" / "預測") to avoid spamming on every
# finding that mentions "Sharpe".
_STRATEGY_KEYWORDS = re.compile(
    r"(strategy|backtest|sharpe|alpha|signal|momentum|mean[\s-]?reversion|"
    r"breakout|imbalance|funding|order[\s-]?flow|"
    r"策略|回測|信號|動能|均值回歸|突破|盤口|資金費率)",
    re.IGNORECASE)
_HYPOTHESIS_SHAPE = re.compile(
    r"(when\s.+\sthen|predicts?|leads?\s+to|preced(es|ing)|signals?|"
    r"當.+則|預測|領先|先於|暗示)",
    re.IGNORECASE)


def looks_like_strategy_idea(text: str) -> bool:
    if not text:
        return False
    return bool(_STRATEGY_KEYWORDS.search(text)
                and _HYPOTHESIS_SHAPE.search(text))


async def maybe_enqueue_synthesis(parent_topic: str, finding: str,
                                   note_id: int | None = None) -> int | None:
    """If finding looks like a strategy idea, queue a synthesis task."""
    if not looks_like_strategy_idea(finding):
        return None
    # Compress the finding into a 1-line hypothesis for the task title.
    hypothesis = finding.strip().replace("\n", " ")[:300]
    title = f"{STRATEGY_TASK_PREFIX} {hypothesis[:120]}"
    desc = (f"Auto-enqueued from finding on '{parent_topic}'.\n"
            f"Source note id: {note_id}\n\n{hypothesis}")
    tid = await kb_sql.add_task(title, description=desc)
    event("strategy_synth_enqueued", task_id=tid, parent_topic=parent_topic,
          note_id=note_id)
    return tid


async def _pick_symbol() -> str:
    """Return the symbol with the most ticks. Defaults to BTCUSDT."""
    from ..kb import duckdb as ddb
    try:
        rows = await ddb.query(
            "SELECT symbol, COUNT(*) AS n FROM ticks "
            "GROUP BY symbol ORDER BY n DESC LIMIT 1")
        if rows and rows[0].get("symbol"):
            return rows[0]["symbol"]
    except Exception as e:
        logger.debug(f"_pick_symbol failed: {e}")
    return "BTCUSDT"


SUGGEST_MOD_PROMPT = (
    "role: trading strategy critic. Given the existing strategy and its "
    "backtest metrics, suggest ONE specific modification likely to improve "
    "the weakest metric (sharpe / mdd / pnl). Reply in <60 words, action-"
    "oriented (e.g. 'add a 200-tick z-score gate to skip trades when |z|<1', "
    "'reduce qty to 0.005', 'switch to log returns'). NO code."
)


async def _suggest_modification(parent_id: int) -> str | None:
    s = await strat_kb.get(int(parent_id))
    bt = await strat_kb.last_backtest(int(parent_id))
    if not s or not bt:
        return None
    m = bt.get("metrics") or {}
    user = (
        f"Strategy: {s['name']}\n"
        f"Hypothesis: {s.get('hypothesis','')}\n"
        f"Code:\n```\n{s['code_python'][:2000]}\n```\n"
        f"Backtest: sharpe={m.get('sharpe',0):.3f}, "
        f"mdd={m.get('max_drawdown',0):.2%}, "
        f"win_rate={m.get('win_rate',0):.2%}, "
        f"pnl={m.get('final_pnl',0):.2f}, "
        f"n_trades={m.get('n_trades',0)}\n\n"
        f"What's the highest-leverage single tweak?")
    resp = await llm.chat(
        [{"role": "system", "content": SUGGEST_MOD_PROMPT},
         {"role": "user", "content": user}],
        temperature=0.5, max_tokens=4096)
    txt = (llm.assistant_text(resp) or "").strip()
    return txt[:400] or None


async def run(task: dict) -> dict:
    """Handle a [design_strategy] task: design → run → iterate → summarise.

    Called from research.loop._run_task when task['topic'] starts with
    STRATEGY_TASK_PREFIX. Returns a dict with a final summary the loop
    can stash on the task row.
    """
    title = task.get("topic") or ""
    hypothesis = title.replace(STRATEGY_TASK_PREFIX, "").strip(" :—")
    if not hypothesis:
        hypothesis = task.get("description") or "untitled hypothesis"
    name = f"auto_{int(time.time()) % 100_000}"
    symbol = await _pick_symbol()

    summary_lines: list[str] = []

    # 1. Design
    designed = await design_strategy(name=name, hypothesis=hypothesis)
    if "error" in designed:
        summary_lines.append(f"design failed: {designed['error']}")
        return {"summary": "\n".join(summary_lines), "status": "design_failed"}
    sid = designed["strategy_id"]
    if designed.get("status") != "ok":
        summary_lines.append(
            f"strategy #{sid} '{name}' saved with compile_error: "
            f"{designed.get('validation_error')}")
        return {"summary": "\n".join(summary_lines),
                "status": "compile_error", "strategy_id": sid}
    summary_lines.append(
        f"designed strategy #{sid} '{name}' on hypothesis: {hypothesis[:120]}")

    # 2. Run
    run1 = await run_strategy(sid, symbol=symbol, max_ticks=20_000)
    if "error" in run1:
        summary_lines.append(f"run failed: {run1['error']}")
        return {"summary": "\n".join(summary_lines),
                "status": "run_failed", "strategy_id": sid}
    m1 = run1.get("metrics") or {}
    summary_lines.append(
        f"backtest #{run1['backtest_id']} ({symbol}, "
        f"{run1['n_ticks']} ticks): sharpe={m1.get('sharpe',0):.3f} "
        f"pnl={m1.get('final_pnl',0):.2f} ({m1.get('final_pnl_pct',0):.2%}), "
        f"mdd={m1.get('max_drawdown',0):.2%}, trades={m1.get('n_trades',0)}")

    # 3. Iterate once if it actually traded
    if (m1.get("n_trades") or 0) > 0:
        mod = await _suggest_modification(sid)
        if mod:
            it = await iterate_strategy(sid, mod)
            if "error" not in it and it.get("status") == "ok":
                child_id = it["strategy_id"]
                run2 = await run_strategy(child_id, symbol=symbol,
                                           max_ticks=20_000)
                if "error" not in run2:
                    m2 = run2.get("metrics") or {}
                    summary_lines.append(
                        f"iterated → #{child_id}, mod={mod[:80]} → "
                        f"sharpe={m2.get('sharpe',0):.3f} "
                        f"pnl={m2.get('final_pnl',0):.2f}")
                else:
                    summary_lines.append(
                        f"iterate ran but child run failed: {run2['error']}")
            else:
                summary_lines.append(
                    f"iterate failed: {it.get('error') or it.get('validation_error')}")
    else:
        summary_lines.append("no trades on first run — skipped iteration")

    # 4. Persist summary as a finding so it shows up in the dashboard
    summary = "\n".join(summary_lines)
    await kb_sql.add_note(
        f"finding/strategy_synth/{name}", summary,
        confidence=0.5,
        confidence_reason="auto-strategy synthesis result",
        source_count=1)
    event("strategy_synth_done", strategy_id=sid, lines=len(summary_lines))
    return {"summary": summary, "status": "ok",
            "strategy_id": sid, "name": name}
