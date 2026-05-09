"""Cross-exchange arbitrage scanner.

Compares the latest trade tick on each exchange for each symbol pair, computes
the gross and net (fee-adjusted) spread in basis points, and persists any
opportunity above threshold to the SQLite ``arb_opportunities`` table.

This module is observation-only. It NEVER places orders — its only outputs are
DB rows, dashboard alerts, and an optional debounced channel ping.

Symbol pairing:
    Binance ``BTCUSDT`` ≈ Coinbase ``BTC-USD``
    Binance ``ETHUSDT`` ≈ Coinbase ``ETH-USD``
USDT/USD ≈ 1.0 is assumed; users tracking the actual peg can read the gross
spread directly.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass

import aiosqlite

from .. import strategies  # noqa: F401 — keep package import warm
from ... import config, runtime
from ...utils.log import event, logger


# Map normalized asset → exchange-specific symbol on each side.
SYMBOL_PAIRS: list[dict] = [
    {"asset": "BTC",
     "binance": "BTCUSDT",
     "coinbase": "BTC-USD"},
    {"asset": "ETH",
     "binance": "ETHUSDT",
     "coinbase": "ETH-USD"},
]

# Per-side taker fee assumed for net spread calc. 0.1% = 10 bp per side,
# so round-trip cost ≈ 20 bp + slippage.
FEE_BP_PER_SIDE = 10.0
DEFAULT_THRESHOLD_BP = 5.0   # net spread must exceed this to fire alert
DEFAULT_INTERVAL_S = 1.0
TICK_FRESHNESS_S = 5.0       # don't compare stale prices


@dataclass
class ArbOpportunity:
    ts: float
    asset: str
    symbol_a: str       # buy here (cheap)
    symbol_b: str       # sell here (expensive)
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    spread_bp: float    # gross
    net_spread_bp: float
    fee_bp: float
    notional_pnl: float  # per 1 unit traded


def compute_spread(buy_price: float, sell_price: float,
                   fee_bp_per_side: float = FEE_BP_PER_SIDE) -> tuple[float, float]:
    """Return (gross_bp, net_bp). Positive net means profitable after fees."""
    if buy_price <= 0:
        return 0.0, 0.0
    gross_bp = (sell_price - buy_price) / buy_price * 10_000.0
    net_bp = gross_bp - 2 * fee_bp_per_side
    return gross_bp, net_bp


class ArbScanner:
    """Background loop that polls runtime.LATEST_TICKS and persists opportunities."""

    def __init__(self, *, threshold_bp: float = DEFAULT_THRESHOLD_BP,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 freshness_s: float = TICK_FRESHNESS_S,
                 fee_bp_per_side: float = FEE_BP_PER_SIDE):
        self.threshold_bp = threshold_bp
        self.interval_s = interval_s
        self.freshness_s = freshness_s
        self.fee_bp_per_side = fee_bp_per_side
        self._stop = asyncio.Event()
        # rolling stats for /api/state and dashboard
        self.last_scan_ts = 0.0
        self.scans = 0
        self.opportunities = 0
        self.last_spreads: dict[str, dict] = {}  # asset → {gross_bp, net_bp, ...}

    @property
    def heartbeat_ok(self) -> bool:
        return self.last_scan_ts and (time.time() - self.last_scan_ts) < 30.0

    def stop(self) -> None:
        self._stop.set()

    async def run(self):
        logger.info(f"arb scanner started (threshold {self.threshold_bp}bp, "
                    f"every {self.interval_s}s)")
        event("arb_scanner_start", threshold_bp=self.threshold_bp)
        while not self._stop.is_set():
            try:
                await self._scan_once()
            except Exception as e:
                logger.warning(f"arb scan crashed: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def _scan_once(self) -> None:
        now = time.time()
        self.last_scan_ts = now
        self.scans += 1
        ticks = runtime.LATEST_TICKS
        for pair in SYMBOL_PAIRS:
            asset = pair["asset"]
            bsym, csym = pair["binance"], pair["coinbase"]
            b = ticks.get(("binance", bsym))
            c = ticks.get(("coinbase", csym))
            if not b or not c:
                continue
            bp, bts = b
            cp, cts = c
            if (now - bts) > self.freshness_s or (now - cts) > self.freshness_s:
                continue
            # Direction: buy on cheaper, sell on richer
            if bp < cp:
                buy_ex, sell_ex, buy_p, sell_p = "binance", "coinbase", bp, cp
                buy_sym, sell_sym = bsym, csym
            else:
                buy_ex, sell_ex, buy_p, sell_p = "coinbase", "binance", cp, bp
                buy_sym, sell_sym = csym, bsym
            gross_bp, net_bp = compute_spread(
                buy_p, sell_p, self.fee_bp_per_side)
            self.last_spreads[asset] = {
                "ts": now, "asset": asset,
                "buy_exchange": buy_ex, "sell_exchange": sell_ex,
                "buy_price": buy_p, "sell_price": sell_p,
                "gross_bp": gross_bp, "net_bp": net_bp,
            }
            if net_bp >= self.threshold_bp:
                opp = ArbOpportunity(
                    ts=now, asset=asset,
                    symbol_a=buy_sym, symbol_b=sell_sym,
                    buy_exchange=buy_ex, sell_exchange=sell_ex,
                    buy_price=buy_p, sell_price=sell_p,
                    spread_bp=gross_bp, net_spread_bp=net_bp,
                    fee_bp=2 * self.fee_bp_per_side,
                    notional_pnl=(sell_p - buy_p) - (
                        buy_p + sell_p) * self.fee_bp_per_side / 10_000.0)
                await self._persist(opp)

    async def _persist(self, opp: ArbOpportunity) -> int:
        async with aiosqlite.connect(config.DB_KB) as db:
            cur = await db.execute(
                """INSERT INTO arb_opportunities
                   (symbol, ts, buy_exchange, sell_exchange, buy_price,
                    sell_price, spread_bp, net_spread_bp, fee_bp, notional_pnl)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (opp.asset, opp.ts, opp.buy_exchange, opp.sell_exchange,
                 opp.buy_price, opp.sell_price, opp.spread_bp,
                 opp.net_spread_bp, opp.fee_bp, opp.notional_pnl))
            await db.commit()
            row_id = cur.lastrowid or 0
        self.opportunities += 1
        event("arb_opportunity",
              id=row_id, asset=opp.asset,
              buy=opp.buy_exchange, sell=opp.sell_exchange,
              gross_bp=round(opp.spread_bp, 2),
              net_bp=round(opp.net_spread_bp, 2))
        try:
            from ...server import event_bus
            event_bus.publish("arb_opportunity", {
                "id": row_id, "asset": opp.asset,
                "buy_exchange": opp.buy_exchange,
                "sell_exchange": opp.sell_exchange,
                "buy_price": opp.buy_price,
                "sell_price": opp.sell_price,
                "spread_bp": round(opp.spread_bp, 2),
                "net_spread_bp": round(opp.net_spread_bp, 2),
                "ts": opp.ts,
            })
        except Exception:
            pass
        return row_id


async def query_history(asset: str | None = None,
                        min_net_bp: float | None = None,
                        since: float | None = None,
                        limit: int = 100) -> list[dict]:
    """Read arb_opportunities. Used by the LLM tool query_arb_history."""
    where, args = [], []
    if asset:
        where.append("symbol = ?")
        args.append(asset)
    if min_net_bp is not None:
        where.append("net_spread_bp >= ?")
        args.append(float(min_net_bp))
    if since is not None:
        where.append("ts >= ?")
        args.append(float(since))
    sql = "SELECT * FROM arb_opportunities"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]
