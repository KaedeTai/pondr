"""Binance partial-book depth WS — depth20@100ms snapshots.

Each message is a full top-20 snapshot, so we just overwrite the OrderBook.
Maintains runtime.BOOKS[(name, symbol)].
"""
from __future__ import annotations
import asyncio
import json
import time
import websockets

from .. import runtime
from ..quant.orderbook.book import OrderBook
from ..utils.log import logger, event

URL = ("wss://stream.binance.com:9443/stream?streams="
       "btcusdt@depth20@100ms/ethusdt@depth20@100ms")
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


class BinanceDepthFeed:
    name = "binance"   # same source label as trade feed for sym pairing

    def __init__(self):
        self.connected = False
        self.last_msg_ts = 0.0
        self.msg_count = 0
        self._stop = asyncio.Event()
        # Pre-create empty books so dashboard knows about them
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
                    self.connected = True
                    backoff = 1.0
                    event("depth_feed_connected", feed=self.label)
                    logger.info(f"{self.label} connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            d = msg.get("data") or msg
                            stream = msg.get("stream", "")
                            sym = stream.split("@")[0].upper() if stream else ""
                            if not sym:
                                continue
                            bids = d.get("bids") or []
                            asks = d.get("asks") or []
                            ts = time.time()
                            book = runtime.BOOKS.setdefault(
                                (self.name, sym),
                                OrderBook(self.name, sym))
                            book.apply_snapshot(bids, asks, ts=ts)
                            self.last_msg_ts = ts
                            self.msg_count += 1
                        except Exception as e:
                            logger.debug(f"binance depth parse: {e}")
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
