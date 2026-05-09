"""Cross-exchange arbitrage scanner — observe-only, never trades."""
from .scanner import ArbScanner, compute_spread, SYMBOL_PAIRS, FEE_BP_PER_SIDE

__all__ = ["ArbScanner", "compute_spread", "SYMBOL_PAIRS", "FEE_BP_PER_SIDE"]
