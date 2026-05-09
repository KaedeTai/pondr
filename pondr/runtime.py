"""Shared runtime registry for live objects (feeds, scanners, books, etc.)."""
from __future__ import annotations

# Live trade feeds (BinanceFeed, CoinbaseFeed, ...)
FEEDS: list = []

# Latest tick price per (source, symbol) — populated by trade feeds, consumed
# by the cross-exchange arb scanner. Value is (price, ts).
LATEST_TICKS: dict[tuple[str, str], tuple[float, float]] = {}

# Live in-memory order books keyed by (source, symbol)
BOOKS: dict = {}

# Singleton scanners/detectors (set by __main__)
ARB_SCANNER = None
IMBALANCE_DETECTOR = None
DEPTH_FEEDS: list = []

# Auxiliary feeds (aggTrade, depth diff stream, ...) — see feeds.AUX_ALL.
AUX_FEEDS: list = []
