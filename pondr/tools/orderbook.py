"""LLM tools for orderbook imbalance — query history + summarize live snapshot."""
from __future__ import annotations
import time

from .. import runtime
from ..kb import duckdb as ddb
from ..quant.orderbook.imbalance import compute_imbalance


# ---- helpers ---------------------------------------------------------------

def _normalize_symbol(symbol: str) -> str:
    """Accept BTC, BTCUSDT, BTC-USD — return uppercase passthrough."""
    return (symbol or "").upper().strip()


# ---- query_orderbook_imbalance --------------------------------------------

async def query_orderbook_imbalance(symbol: str, since: float | None = None,
                                    hours: float | None = None,
                                    threshold: float | None = None,
                                    limit: int = 200) -> dict:
    """Read recent orderbook_imbalances rows.

    threshold filters to |ratio - 1.0| >= threshold (i.e. abs deviation from
    balanced) — catches both bid-heavy and ask-heavy outliers.
    """
    sym = _normalize_symbol(symbol)
    where = []
    if sym:
        # match either underlying form: BTC, BTCUSDT, BTC-USD
        prefixes = {sym, f"{sym}USDT", f"{sym}-USD"}
        formatted = ",".join(f"'{p}'" for p in prefixes)
        where.append(f"symbol IN ({formatted})")
    if since is None and hours is not None:
        since = time.time() - float(hours) * 3600.0
    if since is not None:
        where.append(f"ts >= {float(since)}")
    sql = "SELECT * FROM orderbook_imbalances"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY ts DESC LIMIT {int(limit)}"
    try:
        rows = await ddb.query(sql)
    except Exception as e:
        return {"error": f"duckdb query failed: {e}", "rows": []}
    if threshold is not None:
        thr = float(threshold)
        rows = [r for r in rows
                if abs((r.get("ratio") or 1.0) - 1.0) >= thr]
    ratios = [r.get("ratio") or 1.0 for r in rows]
    return {
        "symbol": sym,
        "count": len(rows),
        "max_ratio": max(ratios) if ratios else None,
        "min_ratio": min(ratios) if ratios else None,
        "rows": rows[:limit],
    }


QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_orderbook_imbalance",
        "description": (
            "Query historical orderbook bid/ask volume imbalance samples. "
            "Ratio > 1 = bid-heavy, < 1 = ask-heavy. Use this to inspect "
            "liquidity skew over time. Symbol can be BTC, BTCUSDT, or "
            "BTC-USD (all map to the same underlying)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "since": {"type": "number",
                          "description": "Unix ts lower bound"},
                "hours": {"type": "number",
                          "description": "Look back this many hours"},
                "threshold": {"type": "number",
                              "description": "Min |ratio-1| to include"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["symbol"],
        },
    },
}


# ---- summarize_orderbook ---------------------------------------------------

async def summarize_orderbook(symbol: str, window_min: int = 5) -> dict:
    """Summary stats for live + recent persisted imbalances on a symbol.

    Includes the current top-20 snapshot from runtime.BOOKS plus rolling stats
    over the last window_min minutes from DuckDB.
    """
    sym = _normalize_symbol(symbol)
    out = {"symbol": sym, "window_min": window_min, "live": [], "history": {}}
    # Live snapshots from any exchange that has this asset
    candidates = {sym, f"{sym}USDT", f"{sym}-USD"}
    for (src, s), book in runtime.BOOKS.items():
        if s in candidates and book.is_fresh(max_age_s=10.0):
            stats = compute_imbalance(book)
            ratio = stats["ratio"]
            if ratio == float("inf"):
                ratio = None
            out["live"].append({
                "source": src, "symbol": s,
                "best_bid": book.best_bid(), "best_ask": book.best_ask(),
                "mid": stats["mid"], "ratio": ratio,
                "bid_vol_top20": stats["bid_vol"],
                "ask_vol_top20": stats["ask_vol"],
                "last_update_age_s": time.time() - book.last_update_ts,
            })
    # Rolling history
    since = time.time() - window_min * 60
    sym_filter = ",".join(f"'{p}'" for p in candidates)
    sql = (f"SELECT ts, source, symbol, ratio, bid_vol, ask_vol, mid_price "
           f"FROM orderbook_imbalances WHERE symbol IN ({sym_filter}) "
           f"AND ts >= {since} ORDER BY ts ASC")
    try:
        rows = await ddb.query(sql)
    except Exception as e:
        out["history_error"] = f"duckdb query failed: {e}"
        rows = []
    if rows:
        ratios = [r["ratio"] or 1.0 for r in rows]
        out["history"] = {
            "n": len(rows),
            "min_ratio": min(ratios),
            "max_ratio": max(ratios),
            "avg_ratio": sum(ratios) / len(ratios),
            "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
        }
    return out


SUMMARIZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "summarize_orderbook",
        "description": (
            "Combined live + recent-history summary of orderbook imbalance "
            "for a symbol. Returns current top-of-book on each exchange and "
            "rolling min/max/avg ratio over the last window_min minutes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "window_min": {"type": "integer", "default": 5},
            },
            "required": ["symbol"],
        },
    },
}
