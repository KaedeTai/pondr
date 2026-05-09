"""run_backtest LLM tool — runs strategy over DuckDB ticks and persists result."""
from __future__ import annotations
import asyncio
import json
import time
import aiosqlite
from .. import config
from ..kb import duckdb as ddb
from ..quant.backtest import run as bt_run, ticks_from_rows, all_metrics, markdown, ascii_curve
from ..quant.strategies import REGISTRY, list_names
from ..utils.log import event, logger

LOW_CONF_THRESHOLD = 1000  # ticks


async def _persist(strategy: str, symbol: str, result, report: str,
                   curve: str) -> int:
    metrics = all_metrics(result)
    confidence = 0.3 if result.n_ticks < LOW_CONF_THRESHOLD else min(
        0.9, 0.4 + (result.n_ticks / 50_000) * 0.5)
    eq_blob = json.dumps({"ts": result.equity_ts[-2000:],
                          "eq": result.equity[-2000:]})
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            """INSERT INTO backtests
               (strategy, symbol, start_ts, end_ts, n_ticks, metrics_json,
                equity_blob, ascii_curve, report_md, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, symbol, result.start_ts, result.end_ts, result.n_ticks,
             json.dumps(metrics), eq_blob, curve, report, confidence))
        await db.commit()
        return cur.lastrowid or 0


async def run_backtest(strategy_name: str, symbol: str,
                       start_ts: float | None = None,
                       end_ts: float | None = None,
                       max_ticks: int = 50_000) -> dict:
    """Run a named strategy over historical DuckDB ticks for a symbol."""
    fn = REGISTRY.get(strategy_name)
    if fn is None:
        return {"error": f"unknown strategy: {strategy_name}",
                "available": list_names()}
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
                "symbol": symbol, "start_ts": start_ts, "end_ts": end_ts}
    try:
        result = await asyncio.to_thread(
            bt_run, fn, ticks, symbol=symbol, strategy_name=strategy_name)
    except Exception as e:
        logger.exception(f"backtest {strategy_name}/{symbol} crashed: {e}")
        return {"error": f"engine crashed: {e!r}"}
    metrics = all_metrics(result)
    report = markdown(result)
    curve = ascii_curve(result.equity)
    bt_id = await _persist(strategy_name, symbol, result, report, curve)
    event("backtest_done", id=bt_id, strategy=strategy_name, symbol=symbol,
          n_ticks=result.n_ticks, sharpe=metrics["sharpe"],
          dd=metrics["max_drawdown"])
    return {
        "id": bt_id, "strategy": strategy_name, "symbol": symbol,
        "n_ticks": result.n_ticks, "metrics": metrics,
        "ascii_curve": curve,
        "report_url": f"/api/backtests/{bt_id}",
        "low_confidence": result.n_ticks < LOW_CONF_THRESHOLD,
    }


SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_backtest",
        "description": (
            "Run a named strategy over historical ticks for a symbol. "
            f"Available strategies: {', '.join(list_names())}. "
            "Symbols come from the ticks table (e.g. BTCUSDT, BTC-USD)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "strategy_name": {"type": "string"},
                "symbol": {"type": "string"},
                "start_ts": {"type": "number"},
                "end_ts": {"type": "number"},
                "max_ticks": {"type": "integer", "default": 50000},
            },
            "required": ["strategy_name", "symbol"],
        },
    },
}
