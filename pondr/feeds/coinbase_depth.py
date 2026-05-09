"""Coinbase ``level2_batch`` WS — incremental L2 book.

The first message per product is a snapshot; subsequent ``l2update`` messages
contain ``changes = [[side, price, size], ...]``. We maintain an OrderBook in
runtime.BOOKS[(name, symbol)].

We use ``level2_batch`` (batched at ~50ms by Coinbase) instead of ``level2``,
which is deprecated for new clients per the task spec.
"""
from __future__ import annotations
import asyncio
import json
import time
import websockets

from .. import runtime
from ..quant.orderbook.book import OrderBook
from ..utils.log import logger, event

URL = "wss://ws-feed.exchange.coinbase.com"
SYMBOLS = ["BTC-USD", "ETH-USD"]
SUB = {"type": "subscribe",
       "product_ids": SYMBOLS,
       "channels": ["level2_batch"]}


def _parse_iso(ts_str: str | None) -> float:
    if not ts_str:
        return time.time()
    try:
        from datetime import datetime
        return datetime.fromisoformat(
            ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


class CoinbaseDepthFeed:
    name = "coinbase"

    def __init__(self):
        self.connected = False
        self.last_msg_ts = 0.0
        self.msg_count = 0
        self._stop = asyncio.Event()
        for s in SYMBOLS:
            runtime.BOOKS[(self.name, s)] = OrderBook(self.name, s)

    @property
    def label(self) -> str:
        return f"{self.name}-depth"

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        URL, ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps(SUB))
                    self.connected = True
                    backoff = 1.0
                    event("depth_feed_connected", feed=self.label)
                    logger.info(f"{self.label} connected")
                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                            t = d.get("type")
                            sym = d.get("product_id") or ""
                            if not sym:
                                continue
                            book = runtime.BOOKS.setdefault(
                                (self.name, sym),
                                OrderBook(self.name, sym))
                            ts = _parse_iso(d.get("time"))
                            if t == "snapshot":
                                # Coinbase top-50 each side; truncate to top-20
                                bids = d.get("bids") or []
                                asks = d.get("asks") or []
                                book.apply_snapshot(
                                    bids[:50], asks[:50], ts=ts)
                            elif t == "l2update":
                                for ch in d.get("changes") or []:
                                    if len(ch) < 3:
                                        continue
                                    side, price, size = ch[0], ch[1], ch[2]
                                    side_norm = "bid" if side == "buy" else "ask"
                                    try:
                                        book.apply_delta(
                                            side_norm, float(price),
                                            float(size), ts=ts)
                                    except (TypeError, ValueError):
                                        continue
                            else:
                                continue
                            self.last_msg_ts = ts
                            self.msg_count += 1
                        except Exception as e:
                            logger.debug(f"coinbase depth parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"{self.label} disconnected: {e}; "
                               f"backoff {backoff:.1f}s")
                event("depth_feed_disconnected",
                      feed=self.label, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
