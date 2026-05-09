"""Binance trade WS — BTCUSDT/ETHUSDT @trade stream."""
from __future__ import annotations
import asyncio
import json
import time
import websockets
from ..kb import duckdb as ddb
from ..utils.log import logger, event
from .. import runtime

URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade"


class BinanceFeed:
    name = "binance"

    def __init__(self):
        self.last_tick = 0.0
        self.tick_count = 0
        self.connected = False
        self._stop = asyncio.Event()

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(URL, ping_interval=20, ping_timeout=20) as ws:
                    self.connected = True
                    backoff = 1.0
                    event("feed_connected", feed=self.name)
                    logger.info(f"{self.name} connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            d = msg.get("data") or msg
                            if d.get("e") != "trade":
                                continue
                            sym = (d.get("s") or "").upper()
                            price = float(d.get("p", 0))
                            qty = float(d.get("q", 0))
                            ts = float(d.get("T", time.time() * 1000)) / 1000.0
                            side = "sell" if d.get("m") else "buy"
                            await ddb.insert_tick(self.name, sym, price, qty, side, ts)
                            runtime.LATEST_TICKS[(self.name, sym)] = (price, ts)
                            self.last_tick = ts
                            self.tick_count += 1
                        except Exception as e:
                            logger.debug(f"binance parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"binance disconnected: {e}; backoff {backoff:.1f}s")
                event("feed_disconnected", feed=self.name, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
