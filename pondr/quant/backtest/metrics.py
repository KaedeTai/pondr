"""Backtest metrics — sharpe, sortino, max drawdown, win rate, profit factor.

Notes on the Sharpe / Sortino fix:

  Tick-level returns are not iid samples in any meaningful sense (one
  tick can be 0.001s away, the next 30s). Computing
  Sharpe = mean(tick_returns)/std(tick_returns) * sqrt(periods/year)
  produced the absurd ±400 numbers we used to see — the periods/year scale
  factor is completely wrong when "period" is a tick.

  Fix: resample equity to fixed-width buckets (default 1h, last value in
  bucket wins), compute returns on the resampled series, and annualise
  with sqrt(buckets_per_year). For BTC-style 24/7 markets this is
  365 * 24 = 8760 hourly buckets per year.
"""
from __future__ import annotations
import math
from typing import Iterable


HOURS_PER_YEAR_24_7 = 365.25 * 24    # crypto trades all the time


def _resample_last(equity: list[float], ts: list[float],
                   bucket_s: float = 3600.0) -> list[float]:
    """Return the last equity value in each bucket of width bucket_s.

    Equity must be monotonic in ts (the engine guarantees this). Empty
    buckets are simply skipped — i.e. we collapse runs of no activity to
    one sample per active bucket. That keeps Sharpe well-defined even on
    sparsely-traded symbols.
    """
    if not equity or not ts or len(equity) != len(ts):
        return []
    out: list[float] = []
    cur_bucket = int(ts[0] // bucket_s)
    cur_val = equity[0]
    for v, t in zip(equity[1:], ts[1:]):
        b = int(t // bucket_s)
        if b != cur_bucket:
            out.append(cur_val)
            cur_bucket = b
        cur_val = v
    out.append(cur_val)
    return out


def _returns(equity: list[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev == 0:
            out.append(0.0)
        else:
            out.append((equity[i] - prev) / abs(prev))
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def sharpe(equity: list[float], ts: list[float] | None = None,
           bucket_s: float = 3600.0,
           periods_per_year: float = HOURS_PER_YEAR_24_7) -> float:
    """Annualised Sharpe on equity resampled to bucket_s-wide buckets.

    If ``ts`` is None we treat each equity sample as one bucket — the caller
    has presumably already resampled. ``periods_per_year`` should match the
    bucket size (8760 for hourly + 24/7 markets).
    """
    if ts is not None and equity:
        eq = _resample_last(equity, ts, bucket_s)
    else:
        eq = list(equity)
    rets = _returns(eq)
    if len(rets) < 2:
        return 0.0
    s = _std(rets)
    if s == 0:
        return 0.0
    return (_mean(rets) / s) * math.sqrt(periods_per_year)


def sortino(equity: list[float], ts: list[float] | None = None,
            bucket_s: float = 3600.0,
            periods_per_year: float = HOURS_PER_YEAR_24_7) -> float:
    if ts is not None and equity:
        eq = _resample_last(equity, ts, bucket_s)
    else:
        eq = list(equity)
    rets = _returns(eq)
    if len(rets) < 2:
        return 0.0
    downs = [r for r in rets if r < 0]
    if not downs:
        return float("inf") if _mean(rets) > 0 else 0.0
    ds = _std(downs)
    if ds == 0:
        return 0.0
    return (_mean(rets) / ds) * math.sqrt(periods_per_year)


def max_drawdown(equity: list[float]) -> float:
    """Return the worst peak-to-trough drawdown as a negative fraction.

    Requires equity values to be > 0 throughout (the engine ensures this
    by starting from initial_capital).
    """
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v - peak) / peak    # always <= 0
            if dd < mdd:
                mdd = dd
    return mdd


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


def _per_trade_pnls(trades) -> list[float]:
    """Take cumulative-realized values from each Trade and diff them."""
    out: list[float] = []
    prev = 0.0
    for tr in trades:
        cur = tr.pnl_realized
        out.append(cur - prev)
        prev = cur
    return out


def all_metrics(result) -> dict:
    """Take a BacktestResult, return a dict of all key metrics."""
    eq = result.equity
    ts = result.equity_ts
    duration = max(1.0, result.end_ts - result.start_ts)
    fees_paid = sum(getattr(t, "fee", 0.0) for t in result.trades)
    closed = _per_trade_pnls(result.trades)
    initial_capital = getattr(result, "initial_capital", 10_000.0) or 10_000.0
    pnl_pct = (result.final_pnl / initial_capital) if initial_capital else 0.0
    return {
        "n_ticks": result.n_ticks,
        "duration_s": duration,
        "n_trades": len(result.trades),
        "initial_capital": initial_capital,
        "final_equity": eq[-1] if eq else initial_capital,
        "final_pnl": result.final_pnl,
        "final_pnl_pct": pnl_pct,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "fees_paid": fees_paid,
        "sharpe": sharpe(eq, ts=ts),
        "sortino": sortino(eq, ts=ts),
        "max_drawdown": max_drawdown(eq),
        "win_rate": win_rate(closed),
        "profit_factor": profit_factor(closed),
        "max_position": result.max_position,
    }
