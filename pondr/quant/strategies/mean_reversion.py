"""Z-score mean-reversion.

When price deviates > entry_z standard deviations from rolling mean, fade it.
Exit when |z| < exit_z.
"""
from __future__ import annotations
import math
from collections import deque
from ..backtest.engine import Signal, Tick

WINDOW = 200
ENTRY_Z = 2.0
EXIT_Z = 0.5


def _zscore(prices: deque) -> float:
    n = len(prices)
    if n < 30:
        return 0.0
    mean = sum(prices) / n
    var = sum((p - mean) ** 2 for p in prices) / max(1, n - 1)
    sd = math.sqrt(var) or 1e-9
    return (prices[-1] - mean) / sd


def strategy(tick: Tick, state: dict) -> Signal | None:
    if "buf" not in state:
        state["buf"] = deque(maxlen=WINDOW)
        state["pos"] = 0
    state["buf"].append(tick.price)
    z = _zscore(state["buf"])
    pos = state["pos"]
    if pos == 0:
        if z > ENTRY_Z:
            state["pos"] = -1
            return Signal("sell", 1.0, f"z={z:.2f}")
        if z < -ENTRY_Z:
            state["pos"] = 1
            return Signal("buy", 1.0, f"z={z:.2f}")
    elif pos == 1 and z > -EXIT_Z:
        state["pos"] = 0
        return Signal("sell", 1.0, "exit long")
    elif pos == -1 and z < EXIT_Z:
        state["pos"] = 0
        return Signal("buy", 1.0, "exit short")
    return None
