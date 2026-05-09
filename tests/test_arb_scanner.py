"""ArbScanner unit tests — synthetic LATEST_TICKS, no network."""
from __future__ import annotations
import asyncio
import time
import pytest

from pondr import runtime
from pondr.quant.arb.scanner import (
    ArbScanner, compute_spread, FEE_BP_PER_SIDE, query_history)


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _fresh_scanner(threshold_bp=5.0):
    s = ArbScanner(threshold_bp=threshold_bp,
                   interval_s=1.0, freshness_s=5.0,
                   fee_bp_per_side=FEE_BP_PER_SIDE)
    return s


def test_compute_spread_basic():
    # buy 100, sell 100.5 → 50 bp gross, 30 bp net (after 20 bp roundtrip)
    g, n = compute_spread(100.0, 100.5, fee_bp_per_side=10.0)
    assert abs(g - 50.0) < 1e-6
    assert abs(n - 30.0) < 1e-6


def test_compute_spread_negative_when_inverted():
    g, n = compute_spread(100.5, 100.0, fee_bp_per_side=10.0)
    assert g < 0
    assert n < 0


def test_scan_below_threshold_no_persist(tmp_path, monkeypatch):
    """Cheap on Binance, slightly more on Coinbase but spread < threshold."""
    # Redirect SQLite to tmp DB so we don't pollute real data
    from pondr import config as cfg
    monkeypatch.setattr(cfg, "DB_KB", tmp_path / "kb.db")
    # Init schema
    import aiosqlite
    aio(_init_kb_schema(cfg.DB_KB))
    runtime.LATEST_TICKS.clear()
    now = time.time()
    runtime.LATEST_TICKS[("binance", "BTCUSDT")] = (50_000.0, now)
    runtime.LATEST_TICKS[("coinbase", "BTC-USD")] = (50_010.0, now)
    s = _fresh_scanner(threshold_bp=10.0)  # 50010/50000 ≈ 2bp gross, < 10
    aio(s._scan_once())
    # No row persisted
    rows = aio(query_history())
    assert len(rows) == 0
    assert s.last_scan_ts > 0
    assert "BTC" in s.last_spreads


def test_scan_above_threshold_persists(tmp_path, monkeypatch):
    from pondr import config as cfg
    monkeypatch.setattr(cfg, "DB_KB", tmp_path / "kb.db")
    aio(_init_kb_schema(cfg.DB_KB))
    runtime.LATEST_TICKS.clear()
    now = time.time()
    # 100 bp gross spread → 80 bp net (well above 5bp threshold)
    runtime.LATEST_TICKS[("binance", "BTCUSDT")] = (50_000.0, now)
    runtime.LATEST_TICKS[("coinbase", "BTC-USD")] = (50_500.0, now)
    s = _fresh_scanner(threshold_bp=5.0)
    aio(s._scan_once())
    rows = aio(query_history(asset="BTC"))
    assert len(rows) == 1
    r = rows[0]
    assert r["buy_exchange"] == "binance"
    assert r["sell_exchange"] == "coinbase"
    assert r["spread_bp"] > 90  # ~100 gross
    assert r["net_spread_bp"] > 70  # ~80 net


def test_scan_skips_stale_ticks(tmp_path, monkeypatch):
    from pondr import config as cfg
    monkeypatch.setattr(cfg, "DB_KB", tmp_path / "kb.db")
    aio(_init_kb_schema(cfg.DB_KB))
    runtime.LATEST_TICKS.clear()
    stale = time.time() - 300  # 5min stale
    runtime.LATEST_TICKS[("binance", "BTCUSDT")] = (50_000.0, stale)
    runtime.LATEST_TICKS[("coinbase", "BTC-USD")] = (50_500.0, stale)
    s = _fresh_scanner(threshold_bp=5.0)
    aio(s._scan_once())
    rows = aio(query_history())
    assert len(rows) == 0


async def _init_kb_schema(path):
    """Bootstrap minimal arb_opportunities schema for tests."""
    import aiosqlite
    async with aiosqlite.connect(path) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS arb_opportunities (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          symbol TEXT NOT NULL,
          ts REAL NOT NULL,
          buy_exchange TEXT,
          sell_exchange TEXT,
          buy_price REAL,
          sell_price REAL,
          spread_bp REAL,
          net_spread_bp REAL,
          fee_bp REAL,
          notional_pnl REAL
        );
        """)
        await db.commit()
