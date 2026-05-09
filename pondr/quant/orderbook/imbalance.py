"""Orderbook imbalance ratios + sustained-anomaly detector.

Imbalance ratio = sum(bid_size_top_N) / sum(ask_size_top_N).
Ratio > 1 means bid-heavy (potential upward pressure), < 1 ask-heavy.

Persists periodic samples to the DuckDB ``orderbook_imbalances`` time-series.
A sustained ratio outside [LOW_THR, HIGH_THR] for ALERT_DURATION_S logs an
``orderbook_alert`` event.
"""
from __future__ import annotations
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from ... import runtime
from ...kb import duckdb as ddb
from ...utils.log import event, logger
from .book import OrderBook


TOP_N_LEVELS = 20
SAMPLE_INTERVAL_S = 1.0
HIGH_THR = 3.0          # bid-heavy alert threshold
LOW_THR = 1.0 / 3.0     # ask-heavy alert threshold (= 0.333…)
ALERT_DURATION_S = 30.0


def compute_imbalance(book: OrderBook, n: int = TOP_N_LEVELS) -> dict:
    bv, av = book.volumes_top_n(n)
    if av <= 0 and bv <= 0:
        return {"ratio": 1.0, "bid_vol": 0.0, "ask_vol": 0.0,
                "levels": n, "mid": book.mid()}
    ratio = bv / av if av > 0 else float("inf")
    return {"ratio": ratio, "bid_vol": bv, "ask_vol": av,
            "levels": min(n, max(len(book.bids), len(book.asks))),
            "mid": book.mid()}


@dataclass
class _Anomaly:
    started_ts: float
    direction: str        # 'bid_heavy' | 'ask_heavy'
    last_ratio: float
    samples: int
    alerted: bool = False


class ImbalanceDetector:
    """Polls runtime.BOOKS, samples imbalance, persists, and alerts."""

    def __init__(self, *, sample_interval_s: float = SAMPLE_INTERVAL_S,
                 top_n: int = TOP_N_LEVELS,
                 high_thr: float = HIGH_THR, low_thr: float = LOW_THR,
                 alert_duration_s: float = ALERT_DURATION_S):
        self.interval = sample_interval_s
        self.top_n = top_n
        self.high_thr = high_thr
        self.low_thr = low_thr
        self.alert_duration_s = alert_duration_s
        self._stop = asyncio.Event()
        self._anom: dict[tuple[str, str], _Anomaly] = {}
        # exposed stats
        self.last_sample_ts = 0.0
        self.samples_persisted = 0
        self.alerts_fired = 0
        self.last_ratios: dict[tuple[str, str], dict] = {}
        # rolling history for ASCII chart (per book key, deque of ratios)
        self.history: dict[tuple[str, str], deque] = defaultdict(
            lambda: deque(maxlen=120))

    @property
    def heartbeat_ok(self) -> bool:
        return self.last_sample_ts and (
            time.time() - self.last_sample_ts) < 30.0

    def stop(self) -> None:
        self._stop.set()

    async def run(self):
        logger.info(f"orderbook imbalance detector started "
                    f"(every {self.interval}s, top {self.top_n} levels)")
        event("orderbook_imbalance_start",
              top_n=self.top_n,
              high_thr=self.high_thr, low_thr=self.low_thr)
        while not self._stop.is_set():
            try:
                await self._sample_once()
            except Exception as e:
                logger.warning(f"imbalance sample crashed: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def _sample_once(self) -> None:
        now = time.time()
        self.last_sample_ts = now
        rows: list[tuple] = []
        for key, book in list(runtime.BOOKS.items()):
            if not book.is_fresh(max_age_s=10.0):
                continue
            stats = compute_imbalance(book, n=self.top_n)
            ratio = stats["ratio"]
            if ratio == float("inf"):
                ratio = 999.0  # cap for storage
            rows.append((now, book.source, book.symbol, ratio,
                         stats["bid_vol"], stats["ask_vol"], stats["levels"],
                         stats["mid"] or 0.0))
            self.last_ratios[key] = {
                "ts": now, "source": book.source, "symbol": book.symbol,
                "ratio": ratio, "bid_vol": stats["bid_vol"],
                "ask_vol": stats["ask_vol"], "mid": stats["mid"]}
            self.history[key].append((now, ratio))
            self._update_anomaly(key, now, ratio)
        if rows:
            try:
                await ddb.insert_orderbook_imbalances(rows)
                self.samples_persisted += len(rows)
            except Exception as e:
                logger.warning(f"imbalance persist: {e}")

    def _update_anomaly(self, key: tuple[str, str],
                        now: float, ratio: float) -> None:
        if ratio >= self.high_thr:
            direction = "bid_heavy"
        elif ratio <= self.low_thr:
            direction = "ask_heavy"
        else:
            # Reset any tracked anomaly that's now over
            self._anom.pop(key, None)
            return
        anom = self._anom.get(key)
        if anom is None or anom.direction != direction:
            self._anom[key] = _Anomaly(
                started_ts=now, direction=direction,
                last_ratio=ratio, samples=1)
            return
        anom.last_ratio = ratio
        anom.samples += 1
        duration = now - anom.started_ts
        if not anom.alerted and duration >= self.alert_duration_s:
            anom.alerted = True
            self.alerts_fired += 1
            event("orderbook_alert",
                  source=key[0], symbol=key[1],
                  direction=direction, ratio=round(ratio, 2),
                  duration_s=round(duration, 1),
                  samples=anom.samples)
            logger.info(f"⚠ orderbook alert: {key} {direction} "
                        f"ratio={ratio:.2f} for {duration:.0f}s")
            try:
                from ...server import event_bus
                event_bus.publish("orderbook_alert", {
                    "source": key[0], "symbol": key[1],
                    "direction": direction,
                    "ratio": round(ratio, 2),
                    "duration_s": round(duration, 1),
                    "samples": anom.samples,
                })
            except Exception:
                pass
