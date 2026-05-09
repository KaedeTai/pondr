"""Kraken trades WS (optional)."""
from __future__ import annotations
import asyncio
import json
import time
import websockets
from ..kb import duckdb as ddb
from ..utils.log import logger, event

URL = "wss://ws.kraken.com/v2"
SUB = {"method": "subscribe",
       "params": {"channel": "trade", "symbol": ["BTC/USD", "ETH/USD"]}}


class KrakenFeed:
    name = "kraken"

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
                            if d.get("channel") != "trade":
                                continue
                            for t in d.get("data") or []:
                                sym = t.get("symbol", "").replace("/", "-")
                                price = float(t.get("price", 0))
                                qty = float(t.get("qty", 0))
                                side = t.get("side", "")
                                ts_str = t.get("timestamp")
                                ts = time.time()
                                if ts_str:
                                    from datetime import datetime
                                    try:
                                        ts = datetime.fromisoformat(
                                            ts_str.replace("Z", "+00:00")).timestamp()
                                    except Exception:
                                        pass
                                await ddb.insert_tick(self.name, sym, price, qty, side, ts)
                                self.last_tick = ts
                                self.tick_count += 1
                        except Exception as e:
                            logger.debug(f"kraken parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"kraken disconnected: {e}; backoff {backoff:.1f}s")
                event("feed_disconnected", feed=self.name, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
