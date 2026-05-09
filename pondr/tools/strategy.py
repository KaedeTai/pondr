"""LLM tools for designing, iterating, running, and comparing strategies.

These four tools let the LLM treat strategy code as just another KB
artifact: invent it from a hypothesis, run it on real ticks, look at the
metrics, and ask itself 'what if I tweak X' — saving the variant as a
child in the lineage tree.

This file is the only path that writes new code into the strategies table.
The sandbox in pondr.quant.strategies.sandbox is responsible for ensuring
the code can't actually do anything dangerous when we eventually run it.
"""
from __future__ import annotations
import asyncio
import json
import time
from typing import Any

import aiosqlite

from .. import config, llm
from ..kb import duckdb as ddb, strategies as strat_kb
from ..quant.backtest import (run as bt_run, ticks_from_rows, all_metrics,
                              markdown, ascii_curve)
from ..quant.strategies.sandbox import compile_strategy, quick_validate
from ..utils.log import event, logger


LOW_CONF_THRESHOLD = 1000

STRATEGY_SYSTEM_PROMPT = """role: trading strategy writer.
Write Python code defining `on_tick(state, tick) -> action` for a backtest engine.

Inputs:
  state: dict you manage across ticks. Engine passes the SAME dict every call,
         starting empty. Initialize fields lazily, e.g.
             if 'prices' not in state:
                 state['prices'] = []
                 state['position'] = 0
  tick: dict {ts, exchange, symbol, side, price, qty} — single trade tick

Output (action) — must be a dict:
  {'side': 'buy' | 'sell' | 'hold' | 'close', 'qty': float, 'reason': str}
    side='hold' means do nothing this tick (qty ignored).
    side='close' flips the engine to flat regardless of current direction.
    qty is in BASE units (e.g. BTC). The engine takes it verbatim — pick a
    sensible sub-1.0 number like 0.001 for BTCUSDT.

HARD RULES — code that violates any of these is rejected before save:
  - NO `import` statements at all. math (built-in), stat (statistics module),
    and np (limited numpy: array/mean/std/median/percentile/log/exp/sqrt/abs/
    diff/cumsum/clip/sign/where/min/max) are pre-injected.
  - NO file or network IO; no eval/exec/open/getattr/setattr/__import__.
  - NO dunder attribute access (foo.__class__ is rejected).
  - Keep total source under 100 lines.
  - on_tick MUST return a dict on every tick (or hold during warmup).

Output ONLY the raw Python source for on_tick. Do not wrap it in
```python``` fences, do not add commentary, do not include 'def __main__'.

Example:
def on_tick(state, tick):
    if 'prices' not in state:
        state['prices'] = []
        state['position'] = 0
    state['prices'].append(tick['price'])
    if len(state['prices']) < 50:
        return {'side': 'hold', 'qty': 0, 'reason': 'warming up'}
    short_ma = sum(state['prices'][-10:]) / 10
    long_ma = sum(state['prices'][-50:]) / 50
    if short_ma > long_ma and state['position'] <= 0:
        state['position'] = 1
        return {'side': 'buy', 'qty': 0.01, 'reason': 'MA crossover up'}
    if short_ma < long_ma and state['position'] >= 0:
        state['position'] = -1
        return {'side': 'sell', 'qty': 0.01, 'reason': 'MA crossover down'}
    return {'side': 'hold', 'qty': 0, 'reason': 'no signal'}
"""


def _strip_code_fences(s: str) -> str:
    """LLMs love adding ``` fences even when told not to. Yank them."""
    s = s.strip()
    if s.startswith("```"):
        # remove first line (```python or just ```)
        lines = s.splitlines()
        if lines:
            lines = lines[1:]
        # remove trailing ``` if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    # Some models wrap with ~~~ instead
    if s.startswith("~~~"):
        lines = s.splitlines()[1:]
        if lines and lines[-1].strip().startswith("~~~"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _extract_code_blob(resp: dict) -> str:
    """Pull code out of an assistant response.

    Local Gemma models routinely burn their entire token budget on
    `message.reasoning_content` and emit empty `content` (finish_reason=
    'length'). When that happens, the actual code is usually present in
    the reasoning trace as bullets / pseudo-code — sometimes verbatim.
    Try `content` first; if empty, scan reasoning for the first def-block.
    """
    txt = llm.assistant_text(resp) or ""
    if txt.strip():
        return txt
    try:
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return ""
    reasoning = (msg.get("reasoning_content") or "").strip()
    if not reasoning:
        return ""
    # Look for a def on_tick block. Sometimes the model writes it inline
    # in the reasoning ("the function would be: def on_tick(state, tick):
    # ..."); we try to lift the def... block all the way to the next blank
    # line or end of string.
    import re
    m = re.search(r"def\s+on_tick\s*\(.+?\)\s*:[\s\S]+?(?=\n\s*\n|\Z)",
                   reasoning)
    if m:
        return m.group(0)
    return ""


async def _ask_llm_for_code(messages: list[dict], temperature: float = 0.4
                             ) -> tuple[str, str]:
    """Returns (raw_text, stripped_code).

    max_tokens is generous (4000) because the local Gemma loves to emit
    >1k tokens of reasoning_content before the actual code. With <2k the
    response gets truncated and content is empty.
    """
    resp = await llm.chat(messages, temperature=temperature, max_tokens=4000)
    txt = _extract_code_blob(resp)
    return txt, _strip_code_fences(txt)


async def design_strategy(name: str, hypothesis: str,
                          source_note_ids: list | None = None) -> dict:
    """Write a brand-new strategy from a hypothesis sentence.

    Returns: {strategy_id, code, validation_error?}.
    Saves to strategies table even if validation fails, with status set so
    we don't lose the LLM's effort — easier to iterate on a broken stub
    than to start from scratch.
    """
    name = (name or "").strip() or f"strat_{int(time.time())}"
    hypothesis = (hypothesis or "").strip()
    if not hypothesis:
        return {"error": "hypothesis is required"}

    user_msg = (f"Hypothesis: {hypothesis}\nName: {name}\n"
                f"Write the on_tick function. Output ONLY the code.")
    raw, code = await _ask_llm_for_code([
        {"role": "system", "content": STRATEGY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    if not code:
        event("design_strategy_fail", name=name, reason="empty_response")
        return {"error": "LLM returned empty code", "raw": raw[:300]}

    err = quick_validate(code)
    status = "ok" if err is None else "compile_error"
    sid = await strat_kb.add(
        name=name, hypothesis=hypothesis,
        code_python=code,
        source_notes=source_note_ids or [],
        created_by="pondr",
        description=hypothesis[:160],
        status=status)
    event("strategy_designed", id=sid, name=name,
          ok=err is None, err=err)
    out = {"strategy_id": sid, "name": name, "hypothesis": hypothesis,
           "code": code, "status": status}
    if err is not None:
        out["validation_error"] = err
    return out


async def iterate_strategy(parent_id: int, modification_request: str
                           ) -> dict:
    """Make a child variant of an existing strategy with the requested mod."""
    parent = await strat_kb.get(int(parent_id))
    if parent is None:
        return {"error": f"unknown parent strategy id {parent_id}"}
    last_bt = await strat_kb.last_backtest(int(parent_id))
    last_summary = "(no backtest yet)"
    if last_bt:
        m = last_bt.get("metrics") or {}
        last_summary = (
            f"symbol={last_bt.get('symbol')} n_ticks={last_bt.get('n_ticks')} "
            f"sharpe={m.get('sharpe',0):.3f} mdd={m.get('max_drawdown',0):.2%} "
            f"pnl={m.get('final_pnl',0):.2f}")

    user_msg = (
        f"Existing strategy ({parent['name']}):\n```\n{parent['code_python']}\n```\n\n"
        f"Recent backtest: {last_summary}\n\n"
        f"Original hypothesis: {parent.get('hypothesis','')}\n\n"
        f"Modification request: {modification_request}\n\n"
        f"Output the modified on_tick code only.")
    raw, code = await _ask_llm_for_code([
        {"role": "system", "content": STRATEGY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    if not code:
        return {"error": "LLM returned empty code", "raw": raw[:300]}

    err = quick_validate(code)
    status = "ok" if err is None else "compile_error"
    new_name = f"{parent['name']}+{int(time.time()) % 10000}"
    new_hyp = f"{parent.get('hypothesis','')[:200]} | mod: {modification_request[:200]}"
    sid = await strat_kb.add(
        name=new_name, hypothesis=new_hyp,
        code_python=code,
        source_notes=[],
        lineage_parent_id=int(parent_id),
        created_by="pondr",
        description=modification_request[:160],
        status=status)
    event("strategy_iterated", id=sid, parent_id=parent_id,
          ok=err is None, err=err)
    out = {"strategy_id": sid, "parent_id": parent_id,
           "name": new_name, "code": code, "status": status,
           "modification_request": modification_request}
    if err is not None:
        out["validation_error"] = err
    return out


async def _persist_backtest(strategy_id: int, name: str, symbol: str,
                            result, report: str, curve: str) -> int:
    metrics = all_metrics(result)
    confidence = 0.3 if result.n_ticks < LOW_CONF_THRESHOLD else min(
        0.9, 0.4 + (result.n_ticks / 50_000) * 0.5)
    eq_blob = json.dumps({"ts": result.equity_ts[-2000:],
                          "eq": result.equity[-2000:]})
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            """INSERT INTO backtests
               (strategy, symbol, start_ts, end_ts, n_ticks, metrics_json,
                equity_blob, ascii_curve, report_md, confidence, strategy_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, symbol, result.start_ts, result.end_ts, result.n_ticks,
             json.dumps(metrics), eq_blob, curve, report, confidence,
             int(strategy_id)))
        await db.commit()
        return cur.lastrowid or 0


async def run_strategy(strategy_id: int, symbol: str = "BTCUSDT",
                       start_ts: float | None = None,
                       end_ts: float | None = None,
                       max_ticks: int = 20_000) -> dict:
    """Pull ticks for symbol, run the strategy in the sandbox, persist + report."""
    s = await strat_kb.get(int(strategy_id))
    if s is None:
        return {"error": f"unknown strategy_id {strategy_id}"}
    if s.get("status") == "compile_error":
        return {"error": "strategy has compile_error — call iterate_strategy first",
                "strategy_id": strategy_id, "name": s.get("name")}
    try:
        on_tick = compile_strategy(s["code_python"])
    except Exception as e:
        await strat_kb.update_status(int(strategy_id), "compile_error")
        return {"error": f"compile failed: {type(e).__name__}: {e}"}

    where = [f"symbol='{symbol}'"]
    if start_ts is not None:
        where.append(f"ts >= {float(start_ts)}")
    if end_ts is not None:
        where.append(f"ts <= {float(end_ts)}")
    sql = (f"SELECT ts, source, symbol, price, qty, side FROM ticks "
           f"WHERE {' AND '.join(where)} ORDER BY ts ASC LIMIT {int(max_ticks)}")
    try:
        rows = await ddb.query(sql)
    except Exception as e:
        return {"error": f"duckdb query failed: {e}"}
    ticks = ticks_from_rows(rows)
    if not ticks:
        return {"error": "no ticks for symbol/range",
                "symbol": symbol, "strategy_id": strategy_id}

    try:
        result = await asyncio.to_thread(
            bt_run, None, ticks, symbol=symbol,
            strategy_name=s["name"], on_tick=on_tick)
    except Exception as e:
        logger.exception(f"strategy {strategy_id} crashed: {e}")
        await strat_kb.update_status(int(strategy_id), "runtime_error")
        return {"error": f"engine crashed: {e!r}"}

    metrics = all_metrics(result)
    report = markdown(result)
    curve = ascii_curve(result.equity)
    bt_id = await _persist_backtest(int(strategy_id), s["name"], symbol,
                                    result, report, curve)
    event("strategy_run", strategy_id=strategy_id, backtest_id=bt_id,
          n_ticks=result.n_ticks, sharpe=metrics["sharpe"],
          mdd=metrics["max_drawdown"], pnl=metrics["final_pnl"])
    return {
        "strategy_id": strategy_id, "name": s["name"],
        "backtest_id": bt_id, "symbol": symbol,
        "n_ticks": result.n_ticks, "metrics": metrics,
        "ascii_curve": curve,
        "report_url": f"/api/backtests/{bt_id}",
        "low_confidence": result.n_ticks < LOW_CONF_THRESHOLD,
    }


async def compare_strategies(strategy_ids: list[int]) -> dict:
    """Side-by-side metrics + ASCII curves for a set of strategies (using
    each strategy's most recent backtest)."""
    if not strategy_ids:
        return {"error": "empty strategy_ids"}
    rows: list[dict] = []
    for sid in strategy_ids:
        s = await strat_kb.get(int(sid))
        if s is None:
            rows.append({"id": sid, "error": "not found"})
            continue
        bt = await strat_kb.last_backtest(int(sid))
        if bt is None:
            rows.append({"id": sid, "name": s["name"],
                         "error": "no backtest yet"})
            continue
        m = bt.get("metrics") or {}
        rows.append({
            "id": sid, "name": s["name"],
            "backtest_id": bt["id"],
            "n_ticks": bt["n_ticks"],
            "sharpe": round(m.get("sharpe", 0), 3),
            "mdd": round(m.get("max_drawdown", 0), 4),
            "win_rate": round(m.get("win_rate", 0), 3),
            "final_pnl": round(m.get("final_pnl", 0), 2),
            "final_pnl_pct": round(m.get("final_pnl_pct", 0), 4),
            "ascii_curve": bt.get("ascii_curve") or "",
        })
    return {"comparison": rows, "n": len(rows)}


# --- OpenAI function-calling schemas ---------------------------------------

DESIGN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "design_strategy",
        "description": (
            "Invent a NEW trading strategy from a 1-line hypothesis. "
            "The LLM (you) writes the on_tick(state, tick) Python code; "
            "it gets validated against the sandbox and saved to the "
            "strategies KB with a fresh strategy_id. Use when the user "
            "(or a research finding) suggests a market regularity worth "
            "testing — e.g. 'large bid imbalance precedes price drops on "
            "BTC'. Follow up with run_strategy(strategy_id, symbol)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Short snake_case identifier"},
                "hypothesis": {"type": "string",
                               "description": "1-2 sentence research idea"},
                "source_note_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Optional kb.notes ids that inspired this",
                },
            },
            "required": ["name", "hypothesis"],
        },
    },
}

ITERATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "iterate_strategy",
        "description": (
            "Create a child variant of an existing strategy by asking the "
            "LLM to apply a specific modification (e.g. 'add a 200-tick "
            "rolling z-score gate', 'reduce position size to 0.005'). "
            "The new strategy is saved with lineage_parent_id pointing to "
            "the original — viewable in the dashboard's lineage tree."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {"type": "integer"},
                "modification_request": {"type": "string"},
            },
            "required": ["parent_id", "modification_request"],
        },
    },
}

RUN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_strategy",
        "description": (
            "Run a saved strategy over historical ticks for a symbol. "
            "Persists a backtest row linked to strategy_id and returns "
            "metrics + ASCII equity curve. Symbols come from the live ticks "
            "table (e.g. BTCUSDT, BTC-USD). Default max_ticks=20000."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_id": {"type": "integer"},
                "symbol": {"type": "string", "default": "BTCUSDT"},
                "start_ts": {"type": "number"},
                "end_ts": {"type": "number"},
                "max_ticks": {"type": "integer", "default": 20000},
            },
            "required": ["strategy_id"],
        },
    },
}

COMPARE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "compare_strategies",
        "description": (
            "Side-by-side comparison of metrics for several strategies "
            "using each one's most recent backtest. Use after running "
            "a parent + a few iterations to pick a winner."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["strategy_ids"],
        },
    },
}
