"""Backtest metrics — sharpe, sortino, max drawdown, win rate, profit factor."""
from __future__ import annotations
import math
from typing import Iterable


def _returns(equity: list[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev == 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / abs(prev) if prev != 0 else 0.0)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(equity: list[float], periods_per_year: float = 31_536_000.0,
           dt_sec: float = 1.0) -> float:
    """Annualized Sharpe — periods_per_year/dt_sec scales tick freq to year."""
    rets = _returns(equity)
    if not rets:
        return 0.0
    s = _std(rets)
    if s == 0:
        return 0.0
    scale = math.sqrt(periods_per_year / max(1e-9, dt_sec))
    return (_mean(rets) / s) * scale


def sortino(equity: list[float], periods_per_year: float = 31_536_000.0,
            dt_sec: float = 1.0) -> float:
    rets = _returns(equity)
    if not rets:
        return 0.0
    downs = [r for r in rets if r < 0]
    if not downs:
        return float("inf") if _mean(rets) > 0 else 0.0
    ds = _std(downs)
    if ds == 0:
        return 0.0
    scale = math.sqrt(periods_per_year / max(1e-9, dt_sec))
    return (_mean(rets) / ds) * scale


def max_drawdown(equity: list[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak == 0:
            continue
        dd = (v - peak) / abs(peak) if peak != 0 else 0.0
        mdd = min(mdd, dd)
    return mdd  # negative number


def win_rate(trade_pnls: Iterable[float]) -> float:
    pnls = list(trade_pnls)
    if not pnls:
        return 0.0
    return sum(1 for p in pnls if p > 0) / len(pnls)


def profit_factor(trade_pnls: Iterable[float]) -> float:
    wins = sum(p for p in trade_pnls if p > 0)
    losses = -sum(p for p in trade_pnls if p < 0)
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def all_metrics(result) -> dict:
    """Take a BacktestResult, return a dict of all key metrics."""
    eq = result.equity
    duration = max(1.0, result.end_ts - result.start_ts)
    dt = duration / max(1, len(eq) - 1)
    trade_pnls = [t.pnl_realized for t in result.trades]
    # Use changes in realized PnL between trades for win/loss
    closed = []
    prev = 0.0
    for p in trade_pnls:
        closed.append(p - prev)
        prev = p
    return {
        "n_ticks": result.n_ticks,
        "duration_s": duration,
        "n_trades": len(result.trades),
        "final_pnl": result.final_pnl,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "sharpe": sharpe(eq, dt_sec=dt),
        "sortino": sortino(eq, dt_sec=dt),
        "max_drawdown": max_drawdown(eq),
        "win_rate": win_rate(closed),
        "profit_factor": profit_factor(closed),
        "max_position": result.max_position,
    }
