"""FRED yield curve poll — needs FRED_API_KEY else skips."""
from __future__ import annotations
import asyncio
from .. import config
from ..tools.rest import rest_call
from ..kb import sqlite as kb_sql
from ..utils.log import logger, event

SERIES = ["DGS2", "DGS10"]
URL = "https://api.stlouisfed.org/fred/series/observations"


async def run(interval_s: int = 3600):
    if not config.FRED_API_KEY:
        logger.info("FRED disabled (no API key)")
        return
    while True:
        try:
            for s in SERIES:
                r = await rest_call(URL, params={
                    "series_id": s, "api_key": config.FRED_API_KEY,
                    "file_type": "json", "limit": 1, "sort_order": "desc"})
                if r.get("status") == 200:
                    obs = (r.get("body") or {}).get("observations") or []
                    if obs:
                        await kb_sql.add_note(f"macro/fred/{s}",
                                              f"{obs[0].get('date')}: {obs[0].get('value')}")
                        event("poll_ok", poller="fred", series=s)
        except Exception as e:
            logger.warning(f"fred poll: {e}")
        await asyncio.sleep(interval_s)
