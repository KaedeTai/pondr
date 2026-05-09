"""Binance diff-depth WS — incremental orderbook updates.

Stream: ``<symbol>@depth@100ms`` — every 100ms each side ships the
levels that changed since the previous update. A real-time orderbook can
be reconstructed by combining a REST snapshot with these diffs (see
Binance's "How to manage a local order book correctly" docs).

For now we simply persist every diff to DuckDB so downstream LLM tools
can replay or summarise queue dynamics; we do NOT attempt to maintain a
local book here (`binance_depth.py` already maintains a top-20 snapshot
book in `runtime.BOOKS`, which is enough for the imbalance scanner).
"""
from __future__ import annotations
import asyncio
import json
import time
import websockets

from ..kb import duckdb as ddb
from ..utils.log import logger, event

URL = ("wss://stream.binance.com:9443/stream?streams="
       "btcusdt@depth@100ms/ethusdt@depth@100ms")


class BinanceDepthDiffFeed:
    name = "binance"

    def __init__(self):
        self.connected = False
        self.last_msg_ts = 0.0
        self.msg_count = 0
        self._stop = asyncio.Event()

    @property
    def label(self) -> str:
        return f"{self.name}-depth-diff"

    async def run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                        URL, ping_interval=20, ping_timeout=20,
                        max_size=8 * 1024 * 1024) as ws:
                    self.connected = True
                    backoff = 1.0
                    event("depth_diff_feed_connected", feed=self.label)
                    logger.info(f"{self.label} connected")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            d = msg.get("data") or msg
                            stream = msg.get("stream", "")
                            sym = (stream.split("@")[0].upper()
                                   if stream else (d.get("s") or "").upper())
                            if not sym:
                                continue
                            first_id = int(d.get("U") or 0)
                            final_id = int(d.get("u") or 0)
                            bids = d.get("b") or []
                            asks = d.get("a") or []
                            ts = time.time()
                            await ddb.insert_depth_diff(
                                self.name, sym, first_id, final_id,
                                json.dumps(bids), json.dumps(asks), ts)
                            self.last_msg_ts = ts
                            self.msg_count += 1
                        except Exception as e:
                            logger.debug(f"binance depth-diff parse: {e}")
            except Exception as e:
                self.connected = False
                logger.warning(f"{self.label} disconnected: {e}; "
                               f"backoff {backoff:.1f}s")
                event("depth_diff_feed_disconnected",
                      feed=self.label, error=repr(e))
                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(60.0, backoff * 2)

    async def stop(self):
        self._stop.set()
