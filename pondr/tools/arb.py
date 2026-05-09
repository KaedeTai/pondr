"""LLM tool: query historical arbitrage opportunities."""
from __future__ import annotations
import time
from ..quant.arb.scanner import query_history


async def query_arb_history(symbol: str | None = None,
                            min_spread_bp: float = 0.0,
                            since: float | None = None,
                            hours: float | None = None,
                            limit: int = 50) -> dict:
    """Look up cross-exchange arb opportunities.

    Symbol is the normalized asset (e.g. ``BTC`` or ``ETH``). ``hours`` is a
    convenience for ``since = now - hours*3600``.
    """
    if since is None and hours is not None:
        since = time.time() - float(hours) * 3600.0
    rows = await query_history(asset=symbol, min_net_bp=min_spread_bp,
                               since=since, limit=limit)
    if not rows:
        return {"count": 0, "rows": [],
                "note": "no opportunities recorded for this filter"}
    nets = [r["net_spread_bp"] or 0 for r in rows]
    grosses = [r["spread_bp"] or 0 for r in rows]
    return {
        "count": len(rows),
        "best_net_bp": max(nets) if nets else 0,
        "median_net_bp": sorted(nets)[len(nets) // 2] if nets else 0,
        "median_gross_bp": sorted(grosses)[len(grosses) // 2] if grosses else 0,
        "rows": rows,
    }


SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_arb_history",
        "description": (
            "Query historical cross-exchange arbitrage opportunities recorded "
            "by the live scanner. Use to see recent inefficiencies between "
            "Binance and Coinbase. Symbol is the normalized asset name (BTC "
            "or ETH). Spreads are reported in basis points (bp), net of an "
            "assumed 10bp/side fee."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "BTC, ETH, ..."},
                "min_spread_bp": {"type": "number", "default": 0.0,
                                  "description": "Filter by min net spread in bp"},
                "since": {"type": "number",
                          "description": "Unix ts lower bound"},
                "hours": {"type": "number",
                          "description": "Look back this many hours"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
}
