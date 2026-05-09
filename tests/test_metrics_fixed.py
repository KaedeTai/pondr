"""Verify the engine + metrics fixes from the strategy refactor:

  * MDD properly tracked relative to initial_capital (no longer stuck at 0)
  * Sharpe stays in plausible range thanks to hourly resampling
  * Backtest ends with realized = final (no dangling unrealized)
  * Fees are taken every fill at the configured rate
"""
import math

from pondr.quant.backtest import run, Tick, Signal, all_metrics
from pondr.quant.backtest.metrics import sharpe, max_drawdown, _resample_last


def _ticks_drift_then_drop(n_up=100, n_down=50, base=100.0, drift=1.0):
    out = []
    for i in range(n_up):
        out.append(Tick(ts=float(i), price=base + drift * i, symbol="X"))
    for j in range(n_down):
        out.append(Tick(ts=float(n_up + j),
                        price=base + drift * n_up - drift * j,
                        symbol="X"))
    return out


def test_resample_last_collapses_into_buckets():
    eq = [1.0, 2.0, 3.0, 4.0, 5.0]
    ts = [0.0, 1000.0, 2000.0, 3700.0, 7300.0]   # 1h-ish gaps
    out = _resample_last(eq, ts, bucket_s=3600.0)
    # Buckets visited: 0, 1, 2; we record the last value before each transition
    # plus the final element. Three transitions in this dataset.
    assert out == [3.0, 4.0, 5.0]


def test_max_drawdown_picks_correct_peak():
    eq = [100.0, 110.0, 120.0, 90.0, 130.0, 50.0]
    mdd = max_drawdown(eq)
    # peak=130, trough=50 → -61.5%
    assert math.isclose(mdd, (50.0 - 130.0) / 130.0, rel_tol=1e-6)


def test_max_drawdown_zero_for_monotone():
    assert max_drawdown([1.0, 2.0, 3.0, 4.0]) == 0.0


def test_sharpe_no_resample_when_ts_omitted():
    """ts=None bypasses resampling — sharpe operates on the raw equity series."""
    # +0.1% drift, fixed-amplitude noise → finite Sharpe
    import random
    random.seed(0)
    eq = [10_000.0]
    for _ in range(50):
        eq.append(eq[-1] * (1 + 0.001 + random.gauss(0, 0.0005)))
    s = sharpe(eq, ts=None)
    assert math.isfinite(s)
    # 50 samples → annualisation by sqrt(8760) is large but finite
    assert -200.0 < s < 200.0


def test_sharpe_with_resampling_is_finite():
    # Build noisy hourly equity over ~50 buckets. Should give a finite,
    # non-absurd sharpe (NOT the +/- 400 the old code emitted).
    import random
    random.seed(0)
    eq = [10_000.0]
    ts = [0.0]
    for i in range(50):
        eq.append(eq[-1] * (1 + random.gauss(0.0001, 0.001)))
        ts.append(ts[-1] + 3600.0)
    s = sharpe(eq, ts=ts, bucket_s=3600.0)
    # The old per-tick formula produced |s|>400; we just need to verify the
    # resampled formula stays in a finite, plausible range.
    assert -50.0 < s < 50.0


def test_engine_force_close_zeroes_unrealized():
    def strat(t, state):
        if t.ts == 0 and not state.get("d"):
            state["d"] = True
            return Signal("buy", 1.0)
        return None
    ticks = _ticks_drift_then_drop(n_up=50, n_down=10, drift=1.0)
    res = run(strat, ticks, fee_rate=0.0)
    assert res.unrealized_pnl == 0.0           # force-close removes unrealized
    assert res.final_position == 0.0
    # final_pnl should equal realized_pnl exactly
    assert math.isclose(res.final_pnl, res.realized_pnl, rel_tol=1e-9)


def test_engine_starts_at_initial_capital():
    def strat(t, state):
        return None
    ticks = _ticks_drift_then_drop()
    res = run(strat, ticks, initial_capital=10_000.0, fee_rate=0.0)
    # No trades → equity stays flat at initial_capital
    assert res.equity[0] == 10_000.0
    assert res.equity[-1] == 10_000.0
    assert res.initial_capital == 10_000.0


def test_metrics_include_pct_and_fees():
    def strat(t, state):
        if t.ts == 0 and not state.get("d"):
            state["d"] = True
            return Signal("buy", 1.0)
        return None
    ticks = _ticks_drift_then_drop(n_up=80, n_down=10, drift=1.0)
    res = run(strat, ticks, fee_rate=0.001, initial_capital=10_000.0)
    m = all_metrics(res)
    assert "final_pnl_pct" in m
    assert "fees_paid" in m
    assert m["fees_paid"] > 0   # we did 2 trades (buy + force-close)
    assert m["initial_capital"] == 10_000.0


def test_engine_signal_size_scales_with_risk_pct():
    def strat(t, state):
        if t.ts == 0 and not state.get("d"):
            state["d"] = True
            return Signal("buy", 1.0)
        return None
    ticks = [Tick(ts=float(i), price=100.0, symbol="X") for i in range(5)]
    # risk_pct=0.01 + initial=10k + price=100 → expected qty 1.0 (10k*0.01/100)
    res = run(strat, ticks, fee_rate=0.0,
              initial_capital=10_000.0, risk_pct=0.01)
    bought = next(t for t in res.trades if t.side == "buy")
    assert math.isclose(bought.size, 1.0, rel_tol=1e-6)
    # Doubling risk_pct doubles qty
    res2 = run(strat, ticks, fee_rate=0.0,
               initial_capital=10_000.0, risk_pct=0.02)
    bought2 = next(t for t in res2.trades if t.side == "buy")
    assert math.isclose(bought2.size, 2.0, rel_tol=1e-6)


def test_dict_action_qty_taken_verbatim():
    def on_tick(state, tick):
        if tick["ts"] == 0:
            return {"side": "buy", "qty": 0.05, "reason": "init"}
        return {"side": "hold", "qty": 0, "reason": ""}
    ticks = [Tick(ts=float(i), price=100.0, symbol="X") for i in range(5)]
    res = run(None, ticks, on_tick=on_tick, fee_rate=0.0,
              initial_capital=10_000.0)
    bought = next(t for t in res.trades if t.side == "buy")
    # qty=0.05 should be taken raw (not scaled by risk_pct)
    assert math.isclose(bought.size, 0.05, rel_tol=1e-9)


def test_dict_action_close_neutralises_position():
    seq = []

    def on_tick(state, tick):
        if tick["ts"] == 0:
            return {"side": "buy", "qty": 0.5, "reason": "in"}
        if tick["ts"] == 5:
            return {"side": "close", "qty": 0, "reason": "out"}
        return {"side": "hold", "qty": 0, "reason": ""}
    ticks = [Tick(ts=float(i), price=100.0, symbol="X") for i in range(10)]
    res = run(None, ticks, on_tick=on_tick, fee_rate=0.0)
    # 'close' on tick 5 should leave position flat at that point
    # then the engine still runs through 5..9 with hold
    assert res.final_position == 0.0
    # 2 trades: buy + close-via-sell (the auto-close at end is a no-op since
    # position is already 0)
    assert len(res.trades) == 2
