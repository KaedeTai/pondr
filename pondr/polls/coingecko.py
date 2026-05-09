"""CoinGecko top-N markets — every 5 min."""
from __future__ import annotations
import asyncio
from ..tools.rest import rest_call
from ..kb import sqlite as kb_sql
from ..utils.log import logger, event

URL = "https://api.coingecko.com/api/v3/coins/markets"


async def run(interval_s: int = 300):
    state = {"last_ok": 0.0, "name": "coingecko"}
    while True:
        try:
            r = await rest_call(URL, params={"vs_currency": "usd", "per_page": 20, "page": 1})
            if r.get("status") == 200 and isinstance(r.get("body"), list):
                top = r["body"][:20]
                summary = ", ".join(f"{c['symbol'].upper()}={c['current_price']}" for c in top[:10])
                await kb_sql.add_note("market/coingecko_top20", summary)
                event("poll_ok", poller="coingecko", n=len(top))
                state["last_ok"] = asyncio.get_event_loop().time()
            else:
                logger.warning(f"coingecko poll bad: {r.get('status')}")
        except Exception as e:
            logger.warning(f"coingecko poll: {e}")
        await asyncio.sleep(interval_s)
