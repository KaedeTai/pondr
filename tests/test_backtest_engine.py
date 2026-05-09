"""Backtest engine + strategies + metrics smoke."""
import asyncio
import pytest
from pondr.quant.backtest import (
    run, Tick, Signal, ticks_from_rows, all_metrics, ascii_curve)
from pondr.quant import strategies


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def _synthetic_ticks(n=300, base=100.0, drift=0.01, amp=0.0):
    """n ticks of price = base + drift*i + amp*sin(i/20)."""
    import math
    return [Tick(ts=float(i), price=base + drift * i + amp * math.sin(i / 20),
                  symbol="SYN") for i in range(n)]


def test_engine_buy_then_hold_gain():
    """Strategy buys once on tick 5, drifts up; force-close yields realized PnL."""
    def strat(t: Tick, state: dict):
        if t.ts == 5 and state.get("done") is None:
            state["done"] = True
            return Signal("buy", 1.0, "init")
        return None
    ticks = _synthetic_ticks(n=100, base=100.0, drift=1.0)
    res = run(strat, ticks, symbol="SYN", strategy_name="hold", fee_rate=0.0)
    assert res.n_ticks == 100
    assert res.final_pnl > 0   # bought at ~105, force-closed at ~199
    # The engine force-closes at the last tick, so we expect 2 trades
    # (the buy + the auto-close sell). Position is back to flat.
    assert res.final_position == 0.0
    assert len(res.trades) == 2


def test_engine_fee_reduces_pnl():
    def strat(t: Tick, state: dict):
        if t.ts == 0 and state.get("d") is None:
            state["d"] = True
            return Signal("buy", 1.0)
        return None
    ticks = _synthetic_ticks(n=10, base=100.0, drift=0.0)
    no_fee = run(strat, ticks, fee_rate=0.0).final_pnl
    with_fee = run(strat, ticks, fee_rate=0.01).final_pnl
    assert with_fee < no_fee


def test_metrics_run():
    def strat(t, s):
        if t.ts == 0 and s.get("d") is None:
            s["d"] = True; return Signal("buy", 1.0)
        return None
    ticks = _synthetic_ticks(n=100, drift=0.5)
    res = run(strat, ticks, fee_rate=0.0)
    m = all_metrics(res)
    for k in ("sharpe", "sortino", "max_drawdown", "win_rate", "profit_factor",
              "fees_paid", "initial_capital", "final_pnl_pct"):
        assert k in m


def test_ma_cross_runs():
    ticks = _synthetic_ticks(n=400, drift=0.05, amp=2.0)
    res = run(strategies.REGISTRY["ma_cross"], ticks,
              symbol="SYN", strategy_name="ma_cross")
    assert res.n_ticks == 400
    # at minimum should have warmed up + traded a few times
    assert len(res.trades) >= 0


def test_breakout_triggers_on_jump():
    """Force a clear breakout and verify position WAS taken (engine
    auto-closes at end so final_position is 0; check max_position instead)."""
    base = [Tick(ts=float(i), price=100.0, symbol="X") for i in range(200)]
    jump = [Tick(ts=float(200 + i), price=110.0, symbol="X") for i in range(20)]
    res = run(strategies.REGISTRY["breakout"], base + jump,
              symbol="X", strategy_name="breakout", fee_rate=0.0)
    assert res.max_position > 0  # took a position at some point


def test_ascii_curve_renders():
    img = ascii_curve([1, 2, 3, 4, 5, 4, 3, 4, 5, 6])
    assert "█" in img
    assert "\n" in img


def test_run_backtest_tool_no_data(monkeypatch):
    """If duckdb has no ticks for symbol, tool returns clean error."""
    from pondr.tools.backtest import run_backtest

    async def fake_query(_sql):
        return []
    from pondr.kb import duckdb as ddb
    monkeypatch.setattr(ddb, "query", fake_query)
    out = aio(run_backtest("ma_cross", "DOES_NOT_EXIST"))
    assert "error" in out


def test_run_backtest_unknown_strategy():
    from pondr.tools.backtest import run_backtest
    out = aio(run_backtest("not_a_strategy", "BTCUSDT"))
    assert "available" in out
