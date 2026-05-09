"""In-memory L2 orderbook for a single (exchange, symbol) pair.

Designed to be cheap to update from a websocket feed. Bids are kept sorted
descending by price, asks ascending. A size of 0 in an update removes the
level. Snapshots from the wire reset both sides.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field


@dataclass
class Level:
    price: float
    size: float


@dataclass
class OrderBook:
    source: str
    symbol: str
    bids: list[Level] = field(default_factory=list)   # desc by price
    asks: list[Level] = field(default_factory=list)   # asc by price
    last_update_ts: float = 0.0

    # ---- snapshot / update -------------------------------------------------
    def apply_snapshot(self, bids: list[tuple[float, float]],
                       asks: list[tuple[float, float]],
                       ts: float | None = None) -> None:
        self.bids = sorted([Level(float(p), float(s)) for p, s in bids
                            if float(s) > 0],
                           key=lambda l: l.price, reverse=True)
        self.asks = sorted([Level(float(p), float(s)) for p, s in asks
                            if float(s) > 0],
                           key=lambda l: l.price)
        self.last_update_ts = ts if ts is not None else time.time()

    def apply_delta(self, side: str, price: float, size: float,
                    ts: float | None = None) -> None:
        """Upsert a level. size==0 removes."""
        levels = self.bids if side == "bid" else self.asks
        # find existing
        for i, lv in enumerate(levels):
            if lv.price == price:
                if size <= 0:
                    levels.pop(i)
                else:
                    levels[i] = Level(price, size)
                self._resort(side)
                self.last_update_ts = ts if ts is not None else time.time()
                return
        if size > 0:
            levels.append(Level(price, size))
            self._resort(side)
        self.last_update_ts = ts if ts is not None else time.time()

    def _resort(self, side: str) -> None:
        if side == "bid":
            self.bids.sort(key=lambda l: l.price, reverse=True)
        else:
            self.asks.sort(key=lambda l: l.price)

    # ---- accessors ---------------------------------------------------------
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def mid(self) -> float | None:
        return mid_price(self.best_bid(), self.best_ask())

    def top_n(self, side: str, n: int) -> list[Level]:
        return (self.bids if side == "bid" else self.asks)[:n]

    def volumes_top_n(self, n: int) -> tuple[float, float]:
        bv = sum(l.size for l in self.bids[:n])
        av = sum(l.size for l in self.asks[:n])
        return bv, av

    def is_fresh(self, max_age_s: float = 5.0) -> bool:
        return self.last_update_ts > 0 and (
            time.time() - self.last_update_ts) <= max_age_s


def mid_price(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0
