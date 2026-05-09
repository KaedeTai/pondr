"""Strategy registry — name → callable."""
from . import ma_cross, mean_reversion, breakout

REGISTRY: dict = {
    "ma_cross": ma_cross.strategy,
    "mean_reversion": mean_reversion.strategy,
    "breakout": breakout.strategy,
}


def get(name: str):
    return REGISTRY.get(name)


def list_names() -> list[str]:
    return sorted(REGISTRY.keys())
