"""Tick-replay backtesting engine.

Strategies are simple callables that consume ticks one-at-a-time and return
optional trade signals. The engine handles position tracking, PnL, and basic
fills (assume immediate execution at tick price + simple fee model).
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class Tick:
    ts: float
    price: float
    qty: float = 0.0
    side: str = ""           # 'buy' / 'sell' / ''
    source: str = ""
    symbol: str = ""


@dataclass
class Signal:
    """Returned by a strategy. action ∈ {'buy', 'sell', 'flat'}."""
    action: str
    size: float = 1.0        # in base units; 1.0 = unit position
    reason: str = ""


@dataclass
class Trade:
    ts: float
    side: str
    price: float
    size: float
    fee: float
    pnl_realized: float = 0.0


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    n_ticks: int
    start_ts: float
    end_ts: float
    final_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    trades: list[Trade]
    equity: list[float]      # equity at each tick (for curve)
    equity_ts: list[float]   # parallel timestamps
    final_position: float
    max_position: float
    metrics: dict = field(default_factory=dict)


Strategy = Callable[[Tick, dict], Signal | None]


def run(strategy: Strategy, ticks: Iterable[Tick], *,
        symbol: str = "?", strategy_name: str = "?",
        fee_rate: float = 0.001) -> BacktestResult:
    """Replay ticks through a strategy. fee_rate is per-side (0.1% default)."""
    state: dict = {}              # strategy can stash state here
    position = 0.0                # signed base units
    cash = 0.0                    # quote currency
    realized = 0.0
    trades: list[Trade] = []
    equity: list[float] = []
    equity_ts: list[float] = []
    n = 0
    start_ts = end_ts = 0.0
    max_pos = 0.0

    for t in ticks:
        n += 1
        if n == 1:
            start_ts = t.ts
        end_ts = t.ts
        signal = strategy(t, state)
        if signal and signal.action != "flat":
            size = max(0.0, float(signal.size))
            if size <= 0:
                pass
            elif signal.action == "buy":
                fee = t.price * size * fee_rate
                cash -= t.price * size + fee
                # if reducing short, realize PnL on closed portion
                if position < 0:
                    closing = min(size, -position)
                    realized += closing * (state.get("avg_entry", t.price) - t.price)
                position += size
                trades.append(Trade(t.ts, "buy", t.price, size, fee, realized))
            elif signal.action == "sell":
                fee = t.price * size * fee_rate
                cash += t.price * size - fee
                if position > 0:
                    closing = min(size, position)
                    realized += closing * (t.price - state.get("avg_entry", t.price))
                position -= size
                trades.append(Trade(t.ts, "sell", t.price, size, fee, realized))
            # track average entry (simple: reset when flipped or flat)
            if position == 0:
                state.pop("avg_entry", None)
            else:
                state["avg_entry"] = t.price  # naive (last fill); fine for MVP
            max_pos = max(max_pos, abs(position))
        # mark-to-market equity
        unreal = position * t.price + cash
        equity.append(unreal)
        equity_ts.append(t.ts)

    final_pnl = (equity[-1] if equity else 0.0)
    unreal = final_pnl - realized
    return BacktestResult(
        strategy=strategy_name, symbol=symbol, n_ticks=n,
        start_ts=start_ts, end_ts=end_ts,
        final_pnl=final_pnl, realized_pnl=realized, unrealized_pnl=unreal,
        trades=trades, equity=equity, equity_ts=equity_ts,
        final_position=position, max_position=max_pos)


def ticks_from_rows(rows: list[dict]) -> list[Tick]:
    """Convert kb.duckdb row dicts to Tick objects, oldest-first."""
    out = [Tick(ts=r["ts"], price=float(r["price"]),
                qty=float(r.get("qty") or 0), side=r.get("side") or "",
                source=r.get("source") or "", symbol=r.get("symbol") or "")
           for r in rows]
    out.sort(key=lambda t: t.ts)
    return out
