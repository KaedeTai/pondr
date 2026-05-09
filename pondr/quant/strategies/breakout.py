"""Range breakout — buy when price > N-period high, sell when < N-period low."""
from __future__ import annotations
from collections import deque
from ..backtest.engine import Signal, Tick

LOOKBACK = 100


def strategy(tick: Tick, state: dict) -> Signal | None:
    if "buf" not in state:
        state["buf"] = deque(maxlen=LOOKBACK)
        state["pos"] = 0
    if len(state["buf"]) < LOOKBACK:
        state["buf"].append(tick.price)
        return None
    hi = max(state["buf"])
    lo = min(state["buf"])
    state["buf"].append(tick.price)
    pos = state["pos"]
    if tick.price > hi and pos <= 0:
        size = 2.0 if pos == -1 else 1.0
        state["pos"] = 1
        return Signal("buy", size, f"break hi {hi:.2f}")
    if tick.price < lo and pos >= 0:
        size = 2.0 if pos == 1 else 1.0
        state["pos"] = -1
        return Signal("sell", size, f"break lo {lo:.2f}")
    return None
