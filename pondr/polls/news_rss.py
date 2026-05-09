"""CoinDesk RSS poll — every 15 min."""
from __future__ import annotations
import asyncio
from ..tools.rest import rest_call
from ..kb import sqlite as kb_sql
from ..utils.log import logger, event

FEEDS = [
    ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
]


async def run(interval_s: int = 900):
    while True:
        try:
            import feedparser
        except Exception:
            logger.warning("feedparser missing")
            return
        for name, url in FEEDS:
            try:
                r = await rest_call(url)
                body = r.get("body")
                if isinstance(body, str):
                    parsed = feedparser.parse(body)
                    items = parsed.entries[:8]
                    txt = " | ".join(getattr(e, "title", "?") for e in items)
                    await kb_sql.add_note(f"news/{name}", txt[:2000])
                    event("poll_ok", poller=f"rss:{name}", n=len(items))
            except Exception as e:
                logger.warning(f"rss poll {name}: {e}")
        await asyncio.sleep(interval_s)
