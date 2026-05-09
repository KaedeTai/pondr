"""Tick-replay backtesting engine.

Two strategy interfaces are supported:

  1. Legacy callable: ``strategy(tick: Tick, state: dict) -> Signal | None``
     used by the hand-written ma_cross / mean_reversion / breakout templates.

  2. LLM-generated dict-style: ``on_tick(state: dict, tick: dict) -> dict``
     where the action dict has keys side / qty / reason. Side is one of
     'buy' | 'sell' | 'hold' | 'close' | 'noop'. Pass it via the ``on_tick``
     kwarg instead of ``strategy``.

Both share the same engine: the dict-style is wrapped into a Signal-returning
adapter at run() entry. Position is signed base units; cash is quote currency
(USDT). The engine starts each backtest from ``initial_capital`` (default
10,000 USDT) so we can compute MDD as a fraction of capital instead of being
stuck at 0% when position-only PnL never crosses zero.

Sizing model:
  - When the legacy callable returns ``Signal('buy', size=1.0)`` the engine
    interprets size as a *fraction of risk_pct of equity* (so size=1.0 +
    risk_pct=0.01 + equity=10k = $100 worth at current price). Size > 1
    scales risk proportionally (this is how ma_cross requests "buy 2" to
    flip from short to long).
  - When the dict-style action specifies ``qty`` the engine takes that
    qty directly in base units (no risk scaling) — the LLM is in charge
    of position sizing, just as the prompt instructs.

End-of-backtest flush: any open position is closed at the last tick price
so realized PnL == final PnL (no dangling unrealized component).
"""
from __future__ import annotations
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
    """Returned by a legacy strategy. action ∈ {'buy', 'sell', 'flat'}."""
    action: str
    size: float = 1.0
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
    final_pnl: float          # realized PnL only (force-closed at end)
    realized_pnl: float
    unrealized_pnl: float     # 0 after force-close, kept for compatibility
    trades: list[Trade]
    equity: list[float]       # equity at each tick (mark-to-market)
    equity_ts: list[float]
    final_position: float
    max_position: float
    initial_capital: float = 10_000.0
    metrics: dict = field(default_factory=dict)


Strategy = Callable[[Tick, dict], Signal | None]
OnTick = Callable[[dict, dict], dict]

DEFAULT_FEE_RATE = 0.0005          # 5 bp per side (Binance maker / spot)
DEFAULT_INITIAL_CAPITAL = 10_000.0
DEFAULT_RISK_PCT = 0.01            # 1% of equity per legacy "size=1" signal


def _signal_from_action(action: dict) -> Signal | None:
    """Translate dict-style on_tick output into a Signal.

    qty in the dict is always taken as raw base units (no risk_pct scaling).
    We mark the size as already-quantified by stuffing it into Signal.size
    and signalling via reason="__abs__" — the engine treats __abs__ in
    reason as 'use size verbatim, skip risk scaling'.
    """
    if not isinstance(action, dict):
        return None
    side = action.get("side")
    qty = float(action.get("qty") or 0.0)
    reason = str(action.get("reason") or "")
    if side in ("hold", "noop", None):
        return None
    if side in ("buy", "sell"):
        if qty <= 0:
            return None
        return Signal(side, qty, "__abs__|" + reason[:120])
    if side == "close":
        # convert to whichever side neutralises the current position; engine
        # handles via marker
        return Signal("__close__", 0.0, reason[:120])
    return None


def run(strategy: Strategy | None = None, ticks: Iterable[Tick] = (), *,
        symbol: str = "?", strategy_name: str = "?",
        on_tick: OnTick | None = None,
        fee_rate: float = DEFAULT_FEE_RATE,
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        risk_pct: float = DEFAULT_RISK_PCT) -> BacktestResult:
    """Replay ticks through a strategy.

    Provide either ``strategy`` (legacy callable) OR ``on_tick`` (dict-style).
    fee_rate is per side (5 bp default ≈ Binance spot maker).
    """
    if strategy is None and on_tick is None:
        raise ValueError("must pass either strategy or on_tick")

    state: dict = {}
    position = 0.0                # signed base units
    cash = float(initial_capital)
    realized = 0.0
    avg_entry = 0.0               # tracked properly across same-side adds
    trades: list[Trade] = []
    equity: list[float] = []
    equity_ts: list[float] = []
    n = 0
    start_ts = end_ts = 0.0
    max_pos = 0.0
    last_price = 0.0

    def _equity_now(price: float) -> float:
        return cash + position * price

    def _fill(side: str, fill_qty: float, price: float) -> None:
        """Execute a fill, update cash/position/realized/trades."""
        nonlocal position, cash, realized, avg_entry
        if fill_qty <= 0:
            return
        fee = price * fill_qty * fee_rate
        if side == "buy":
            cash -= price * fill_qty + fee
            if position < 0:
                # closing short
                closing = min(fill_qty, -position)
                realized += closing * (avg_entry - price)
                opening = fill_qty - closing
                if opening > 0:
                    # flipped to long; new avg = price
                    avg_entry = price
                position += fill_qty
                if abs(position) < 1e-12:
                    avg_entry = 0.0
            else:
                # adding to (or opening) long
                if position == 0:
                    avg_entry = price
                else:
                    avg_entry = (avg_entry * position + price * fill_qty) / (
                        position + fill_qty)
                position += fill_qty
        elif side == "sell":
            cash += price * fill_qty - fee
            if position > 0:
                closing = min(fill_qty, position)
                realized += closing * (price - avg_entry)
                opening = fill_qty - closing
                if opening > 0:
                    avg_entry = price
                position -= fill_qty
                if abs(position) < 1e-12:
                    avg_entry = 0.0
            else:
                # adding to (or opening) short
                if position == 0:
                    avg_entry = price
                else:
                    # weighted entry across same-side stack
                    avg_entry = (avg_entry * (-position) + price * fill_qty) / (
                        -position + fill_qty)
                position -= fill_qty
        trades.append(Trade(
            ts=last_ts, side=side, price=price, size=fill_qty,
            fee=fee, pnl_realized=realized))

    last_ts = 0.0

    for t in ticks:
        n += 1
        if n == 1:
            start_ts = t.ts
        end_ts = last_ts = t.ts
        last_price = t.price

        # Run the strategy
        if on_tick is not None:
            tick_dict = {
                "ts": t.ts, "exchange": t.source, "symbol": t.symbol,
                "side": t.side, "price": t.price, "qty": t.qty,
            }
            try:
                action = on_tick(state, tick_dict)
            except Exception:
                action = None
            signal = _signal_from_action(action) if action else None
        else:
            try:
                signal = strategy(t, state)  # type: ignore[misc]
            except Exception:
                signal = None

        if signal is not None:
            if signal.action == "__close__":
                if position > 0:
                    _fill("sell", position, t.price)
                elif position < 0:
                    _fill("buy", -position, t.price)
            elif signal.action in ("buy", "sell"):
                size_raw = max(0.0, float(signal.size))
                if size_raw > 0:
                    if (signal.reason or "").startswith("__abs__"):
                        # dict-style: qty is absolute base units
                        fill_qty = size_raw
                    else:
                        # legacy: size is multiples of risk_pct
                        equity_now = _equity_now(t.price)
                        notional = equity_now * risk_pct * size_raw
                        fill_qty = (notional / t.price) if t.price > 0 else 0
                    _fill(signal.action, fill_qty, t.price)
            max_pos = max(max_pos, abs(position))

        equity.append(_equity_now(t.price))
        equity_ts.append(t.ts)

    # Force-close at last price so realized = final, no dangling unrealized.
    if abs(position) > 1e-12 and last_price > 0:
        if position > 0:
            _fill("sell", position, last_price)
        else:
            _fill("buy", -position, last_price)
        # Adjust the last equity sample to the post-close value.
        if equity:
            equity[-1] = _equity_now(last_price)

    final_equity = equity[-1] if equity else initial_capital
    realized_pnl = final_equity - initial_capital
    return BacktestResult(
        strategy=strategy_name, symbol=symbol, n_ticks=n,
        start_ts=start_ts, end_ts=end_ts,
        final_pnl=realized_pnl,
        realized_pnl=realized_pnl,
        unrealized_pnl=0.0,
        trades=trades, equity=equity, equity_ts=equity_ts,
        final_position=position, max_position=max_pos,
        initial_capital=float(initial_capital))


def ticks_from_rows(rows: list[dict]) -> list[Tick]:
    """Convert kb.duckdb row dicts to Tick objects, oldest-first."""
    out = [Tick(ts=r["ts"], price=float(r["price"]),
                qty=float(r.get("qty") or 0), side=r.get("side") or "",
                source=r.get("source") or "", symbol=r.get("symbol") or "")
           for r in rows]
    out.sort(key=lambda t: t.ts)
    return out
