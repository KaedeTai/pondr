"""Binance aggTrade WS — BTCUSDT/ETHUSDT @aggTrade stream.

aggTrade events combine consecutive trades from the same taker order at the
same price into a single event. Smaller volume than raw trade stream; widely
used by quant pipelines for VWAP / signed-flow analysis.
"""
from __future__ import annotations
import asyncio
import json
import time
import websockets

from ..kb import duckdb as ddb
from ..utils.log import logger, event

URL = ("wss://stream.binance.com:9443/stream?streams="
       "btcusdt@aggTrade/ethusdt@aggTrade")


class BinanceAggTradeFeed:
    name = "binance"  # same source label so symbol pairing aligns

    def __init__(self):
        self.connected = False
        self.last_msg_ts = 0.0
        self.msg_count = 0
        self._stop = asyncio.Event()

    @property
    def label(self) -> str:
        return f"{self.name}-aggtrade"

    # ``__main__`` heartbeat reads ``tick_count`` from trade feeds. Mirror
    # that attribute so the aggTrade feed can plug into the same surfaces.
    @property
    def tick_count(self) -> int:
        return self.msg_count

    @property
    def last_tick(self) -> float:
        return self.last_msg_ts

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        URL, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    backoff = 1.0
                    event("aggtrade_feed_connected", feed=self.label)
                    logger.info(f"{self.label} connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            d = msg.get("data") or msg
                            if d.get("e") != "aggTrade":
                                continue
                            sym = (d.get("s") or "").upper()
                            agg_id = int(d.get("a") or 0)
                            first_id = int(d.get("f") or 0)
                            last_id = int(d.get("l") or 0)
                            price = float(d.get("p", 0) or 0)
                            qty = float(d.get("q", 0) or 0)
                            ts = float(d.get("T", time.time() * 1000)) / 1000.0
                            is_buyer_maker = bool(d.get("m"))
                            await ddb.insert_aggtrade(
                                self.name, sym, agg_id, first_id, last_id,
                                price, qty, is_buyer_maker, ts)
                            self.last_msg_ts = ts
                            self.msg_count += 1
                        except Exception as e:
                            logger.debug(f"binance aggtrade parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"{self.label} disconnected: {e}; "
                               f"backoff {backoff:.1f}s")
                event("aggtrade_feed_disconnected",
                      feed=self.label, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
