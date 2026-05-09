"""Markdown + ASCII report generator for backtest results."""
from __future__ import annotations
from .metrics import all_metrics


def ascii_curve(equity: list[float], width: int = 60, height: int = 12) -> str:
    if not equity:
        return "(empty)"
    lo, hi = min(equity), max(equity)
    rng = hi - lo or 1.0
    # downsample to width
    step = max(1, len(equity) // width)
    series = equity[::step][:width]
    grid = [[" "] * width for _ in range(height)]
    for x, v in enumerate(series):
        y = int((1 - (v - lo) / rng) * (height - 1))
        y = max(0, min(height - 1, y))
        grid[y][x] = "█"
    return "\n".join("".join(row) for row in grid)


def markdown(result) -> str:
    m = all_metrics(result)
    out = [f"# Backtest: {result.strategy} on {result.symbol}",
           f"_n_ticks_={m['n_ticks']}, _duration_={m['duration_s']:.0f}s, "
           f"_n_trades_={m['n_trades']}", "",
           "## Metrics", ""]
    out.append("| metric | value |")
    out.append("|---|---|")
    for k in ("final_pnl", "realized_pnl", "unrealized_pnl",
              "sharpe", "sortino", "max_drawdown",
              "win_rate", "profit_factor", "max_position"):
        v = m[k]
        if isinstance(v, float):
            out.append(f"| {k} | {v:.4f} |")
        else:
            out.append(f"| {k} | {v} |")
    out.append("")
    out.append("## Equity curve")
    out.append("```")
    out.append(ascii_curve(result.equity))
    out.append("```")
    return "\n".join(out)
