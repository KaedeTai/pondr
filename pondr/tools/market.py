"""Market-data tools backed by DuckDB ticks."""
from __future__ import annotations
import json
import time
from statistics import mean, pstdev
from ..kb import duckdb as ddb


async def read_market_ticks(symbol: str, since: float | None = None,
                            limit: int = 100) -> list[dict]:
    return await ddb.recent(symbol, since=since, limit=limit)


async def read_aggtrades(symbol: str, since: float | None = None,
                         limit: int = 100) -> list[dict]:
    """Recent aggTrade events for a symbol from DuckDB."""
    return await ddb.recent_aggtrades(symbol, since=since, limit=limit)


async def read_depth_diffs(symbol: str, since: float | None = None,
                           limit: int = 100) -> list[dict]:
    """Recent depth-diff events for a symbol from DuckDB.

    bids/asks come back as JSON strings (DuckDB JSON column); decoded into
    lists of [price, qty] pairs for caller convenience."""
    rows = await ddb.recent_depth_diffs(symbol, since=since, limit=limit)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("bids", "asks"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except Exception:
                    d[k] = []
        out.append(d)
    return out


async def summarize_aggtrades(symbol: str, window_min: int = 5) -> dict:
    """VWAP / signed-flow summary over recent aggTrade events.

    is_buyer_maker=True means the aggressive side was a SELL hitting the bid;
    so the *buy volume* is the qty of trades where is_buyer_maker is False.
    """
    since = time.time() - window_min * 60
    rows = await ddb.recent_aggtrades(symbol, since=since, limit=20000)
    if not rows:
        return {"symbol": symbol, "window_min": window_min, "n_trades": 0}
    prices = [r["price"] for r in rows if r.get("price") is not None]
    qtys = [r["qty"] for r in rows if r.get("qty") is not None]
    if not prices or not qtys:
        return {"symbol": symbol, "window_min": window_min,
                "n_trades": len(rows)}
    notional = sum(p * q for p, q in zip(prices, qtys))
    total_qty = sum(qtys)
    vwap = (notional / total_qty) if total_qty else 0.0
    buy_qty = sum(r["qty"] for r in rows
                  if r.get("qty") is not None and not r.get("is_buyer_maker"))
    sell_qty = sum(r["qty"] for r in rows
                   if r.get("qty") is not None and r.get("is_buyer_maker"))
    buy_ratio = (buy_qty / total_qty) if total_qty else 0.0
    return {
        "symbol": symbol,
        "window_min": window_min,
        "n_trades": len(rows),
        "vwap": vwap,
        "total_qty": total_qty,
        "notional": notional,
        "buy_qty": buy_qty,
        "sell_qty": sell_qty,
        "buy_vol_ratio": buy_ratio,
        "price_first": prices[-1],
        "price_last": prices[0],
        "price_min": min(prices),
        "price_max": max(prices),
    }


async def summarize_market(symbol: str, window_min: int = 5) -> dict:
    since = time.time() - window_min * 60
    rows = await ddb.recent(symbol, since=since, limit=10000)
    if not rows:
        return {"symbol": symbol, "window_min": window_min, "count": 0}
    prices = [r["price"] for r in rows if r.get("price") is not None]
    if not prices:
        return {"symbol": symbol, "window_min": window_min, "count": len(rows)}
    return {
        "symbol": symbol,
        "window_min": window_min,
        "count": len(prices),
        "price_first": prices[-1],
        "price_last": prices[0],
        "price_min": min(prices),
        "price_max": max(prices),
        "price_mean": mean(prices),
        "price_std": pstdev(prices) if len(prices) > 1 else 0.0,
        "ret_pct": ((prices[0] - prices[-1]) / prices[-1] * 100.0) if prices[-1] else 0.0,
    }


READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_market_ticks",
        "description": "Recent market ticks for a symbol (e.g. BTCUSDT, BTC-USD).",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "since": {"type": "number", "description": "Unix ts lower bound"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["symbol"],
        },
    },
}
SUMMARIZE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "summarize_market",
        "description": "Summary stats for a symbol over a window of recent minutes.",
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

READ_AGGTRADES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_aggtrades",
        "description": ("Recent Binance aggTrade events for a symbol. "
                        "aggTrades collapse consecutive same-price taker fills "
                        "from a single order; useful for VWAP/signed-flow."),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "since": {"type": "number",
                          "description": "Unix ts lower bound"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["symbol"],
        },
    },
}

READ_DEPTH_DIFFS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_depth_diffs",
        "description": ("Recent Binance diff-depth updates for a symbol "
                        "(incremental orderbook deltas at 100ms cadence). "
                        "bids/asks are decoded JSON [price, qty] lists."),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "since": {"type": "number"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["symbol"],
        },
    },
}

SUMMARIZE_AGGTRADES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "summarize_aggtrades",
        "description": ("VWAP / buy-volume-ratio / price stats over recent "
                        "aggTrades for a symbol over a window of minutes."),
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
