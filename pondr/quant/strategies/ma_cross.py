"""Short/long moving-average crossover.

Long when short-MA crosses above long-MA, flat-then-short on opposite cross.
Maintains a simple position of -1, 0, or +1.
"""
from __future__ import annotations
from collections import deque
from ..backtest.engine import Signal, Tick

SHORT_W = 20
LONG_W = 100


def strategy(tick: Tick, state: dict) -> Signal | None:
    # Lazily init buffers
    if "short" not in state:
        state["short"] = deque(maxlen=SHORT_W)
        state["long"] = deque(maxlen=LONG_W)
        state["pos"] = 0  # -1, 0, +1
    state["short"].append(tick.price)
    state["long"].append(tick.price)
    if len(state["long"]) < LONG_W:
        return None
    s = sum(state["short"]) / len(state["short"])
    l = sum(state["long"]) / len(state["long"])
    pos = state["pos"]
    if s > l and pos <= 0:
        # go long: if currently short, buy 2 (cover + open); else buy 1
        size = 2.0 if pos == -1 else 1.0
        state["pos"] = 1
        return Signal("buy", size, "ma_cross up")
    if s < l and pos >= 0:
        size = 2.0 if pos == 1 else 1.0
        state["pos"] = -1
        return Signal("sell", size, "ma_cross down")
    return None
