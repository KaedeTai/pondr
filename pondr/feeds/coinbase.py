"""Coinbase Advanced Trade matches WS — BTC-USD/ETH-USD."""
from __future__ import annotations
import asyncio
import json
import time
import websockets
from ..kb import duckdb as ddb
from ..utils.log import logger, event
from .. import runtime

URL = "wss://ws-feed.exchange.coinbase.com"
SUB = {"type": "subscribe",
       "product_ids": ["BTC-USD", "ETH-USD"],
       "channels": ["matches"]}


class CoinbaseFeed:
    name = "coinbase"

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
                    await ws.send(json.dumps(SUB))
                    self.connected = True
                    backoff = 1.0
                    event("feed_connected", feed=self.name)
                    logger.info(f"{self.name} connected")
                    async for raw in ws:
                        try:
                            d = json.loads(raw)
                            if d.get("type") != "match":
                                continue
                            sym = d.get("product_id", "")
                            price = float(d.get("price", 0))
                            qty = float(d.get("size", 0))
                            side = d.get("side", "")
                            ts_str = d.get("time")
                            if ts_str:
                                from datetime import datetime
                                try:
                                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                    ts = dt.timestamp()
                                except Exception:
                                    ts = time.time()
                            else:
                                ts = time.time()
                            await ddb.insert_tick(self.name, sym, price, qty, side, ts)
                            runtime.LATEST_TICKS[(self.name, sym)] = (price, ts)
                            self.last_tick = ts
                            self.tick_count += 1
                        except Exception as e:
                            logger.debug(f"coinbase parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"coinbase disconnected: {e}; backoff {backoff:.1f}s")
                event("feed_disconnected", feed=self.name, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
