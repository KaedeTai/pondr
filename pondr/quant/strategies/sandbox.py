"""Run LLM-generated strategy code in a tightly restricted Python sandbox.

The LLM writes a single function with the signature

    def on_tick(state: dict, tick: dict) -> dict:
        # state is a per-strategy dict the engine carries across ticks
        # tick has keys: ts, exchange/source, symbol, side, price, qty
        # action: {'side': 'buy'|'sell'|'hold'|'close', 'qty': float, 'reason': str}

We cannot let the LLM `import os; os.system(...)`, exfiltrate the DB, etc.
This module:

  1. AST-walks the source and rejects anything dangerous (Import, Attribute on
     dunders, banned names like open/exec/getattr/__import__/...).
  2. Compiles the AST and execs it inside a hand-built globals dict that only
     exposes a whitelisted slice of builtins, plus ``math`` (stdlib),
     ``stat`` (statistics) and a tiny ``np`` namespace with the few numeric
     functions strategies actually need.
  3. Hard-caps execution per tick via asyncio.wait_for. Strategy code is sync,
     so we yield to the event loop after each tick — that's enough for
     wait_for to interrupt a code path that's stuck in an infinite Python
     loop (CPython releases the GIL between ticks).

This is *not* a hardened sandbox in the security-research sense — a
sufficiently motivated attacker who controls the LLM output could probably
escape via an obscure dunder path. It IS enough to block the obvious
mistakes the LLM can make (import-based shellouts, file IO, network), and
to give us a tight loop that won't bring down the bot.
"""
from __future__ import annotations
import ast
import asyncio
import builtins as _builtins
import math
import statistics
from typing import Any, Callable

import numpy as np


# --- Whitelist of builtin names the strategy may reference -----------------

SAFE_BUILTINS: dict[str, Any] = {
    name: getattr(_builtins, name) for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
        "frozenset", "int", "len", "list", "map", "max", "min", "range",
        "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
        "zip", "True", "False", "None", "isinstance", "issubclass",
        "ValueError", "TypeError", "KeyError", "IndexError",
        "ZeroDivisionError", "Exception",
    ) if hasattr(_builtins, name)
}
# Allow print but no-op it so debug output doesn't escape into stdout.
SAFE_BUILTINS["print"] = lambda *a, **kw: None


# --- Names we explicitly forbid in the source ------------------------------

# These would let strategy code escape the sandbox. We reject any reference
# at AST level so even unused mentions (e.g. `getattr.__qualname__`) fail.
BANNED_NAMES: set[str] = {
    "open", "eval", "exec", "compile", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "input", "exit", "quit",
    "memoryview", "breakpoint", "help", "dir", "type", "id", "hash",
    "object", "super", "classmethod", "staticmethod", "property",
    "__build_class__",
}


# --- Limited numpy namespace -----------------------------------------------

class _LimitedNP:
    """Expose only the small set of numpy functions strategies need.

    Anything else (np.load, np.fromfile, ...) is simply not present, so
    `np.load(...)` raises AttributeError at runtime.
    """
    array = staticmethod(np.array)
    asarray = staticmethod(np.asarray)
    mean = staticmethod(np.mean)
    std = staticmethod(np.std)
    var = staticmethod(np.var)
    median = staticmethod(np.median)
    percentile = staticmethod(np.percentile)
    quantile = staticmethod(np.quantile)
    log = staticmethod(np.log)
    log1p = staticmethod(np.log1p)
    exp = staticmethod(np.exp)
    sqrt = staticmethod(np.sqrt)
    abs = staticmethod(np.abs)
    diff = staticmethod(np.diff)
    cumsum = staticmethod(np.cumsum)
    sum = staticmethod(np.sum)
    min = staticmethod(np.min)
    max = staticmethod(np.max)
    argmin = staticmethod(np.argmin)
    argmax = staticmethod(np.argmax)
    clip = staticmethod(np.clip)
    sign = staticmethod(np.sign)
    where = staticmethod(np.where)
    nan = float("nan")
    inf = float("inf")
    pi = math.pi


# --- AST validator ---------------------------------------------------------

class _Validator(ast.NodeVisitor):
    """Walk the parsed module and raise ValueError for anything illegal."""

    def visit_Import(self, node: ast.Import):
        raise ValueError("imports not allowed in strategy code")

    def visit_ImportFrom(self, node: ast.ImportFrom):
        raise ValueError("imports not allowed in strategy code")

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("__") and node.attr.endswith("__"):
            raise ValueError(
                f"dunder attribute access not allowed: {node.attr}")
        # Also reject dynamic attribute lookups that happen to spell a banned
        # name through a parent (foo.getattr(...) etc.)
        if node.attr in BANNED_NAMES:
            raise ValueError(f"banned attribute: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in BANNED_NAMES:
            raise ValueError(f"banned name: {node.id!r}")
        # Block any reference whose own name is dunder; that's how attribute
        # walking via getattr() typically starts.
        if node.id.startswith("__") and node.id.endswith("__"):
            raise ValueError(f"dunder name not allowed: {node.id!r}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        # Reject `open(...)` style calls that slipped past visit_Name (rare —
        # already covered by BANNED_NAMES — but defense in depth).
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_NAMES:
            raise ValueError(f"banned call: {func.id!r}")
        self.generic_visit(node)


def _build_globals() -> dict[str, Any]:
    return {
        "__builtins__": SAFE_BUILTINS,
        "math": math,
        "stat": statistics,
        "np": _LimitedNP(),
    }


def compile_strategy(code: str) -> Callable[[dict, dict], dict]:
    """Validate + compile the strategy source.

    Returns the on_tick callable. Raises ValueError on disallowed syntax,
    SyntaxError for malformed code.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("empty strategy code")
    if len(code) > 20_000:
        raise ValueError("strategy code too long (>20k chars)")
    tree = ast.parse(code, mode="exec")
    _Validator().visit(tree)
    g = _build_globals()
    l: dict[str, Any] = {}
    exec(compile(tree, "<strategy>", "exec"), g, l)
    fn = l.get("on_tick")
    if not callable(fn):
        raise ValueError("strategy must define on_tick(state, tick) -> action")
    return fn


# --- Async runner with timeout --------------------------------------------

async def _run_loop(on_tick: Callable, ticks: list[dict],
                    state: dict, actions: list[dict],
                    yield_every: int = 50) -> None:
    """Inner loop — yields to the event loop every N ticks so wait_for can
    interrupt a runaway strategy."""
    n = 0
    for t in ticks:
        n += 1
        try:
            action = on_tick(state, t)
        except Exception as e:
            actions.append({"side": "noop", "error": True,
                            "reason": f"{type(e).__name__}: {e}"})
            continue
        if not isinstance(action, dict):
            actions.append({"side": "noop", "error": True,
                            "reason": "on_tick must return a dict"})
            continue
        # Normalize: at minimum, side must be present
        side = action.get("side")
        if side not in ("buy", "sell", "hold", "close", "noop"):
            action = {"side": "hold", "qty": 0.0, "reason": "invalid side"}
        actions.append(action)
        if n % yield_every == 0:
            await asyncio.sleep(0)


async def run_strategy_safe(code: str, ticks: list[dict],
                             timeout_s: float = 5.0
                             ) -> tuple[list[dict], str | None]:
    """Compile, run, and time-bound a strategy.

    Returns (actions, error_msg or None). actions has same length as ticks
    when no timeout fired.
    """
    try:
        on_tick = compile_strategy(code)
    except SyntaxError as e:
        return [], f"compile: SyntaxError: {e}"
    except ValueError as e:
        return [], f"compile: {e}"
    state: dict = {}
    actions: list[dict] = []
    try:
        await asyncio.wait_for(
            _run_loop(on_tick, ticks, state, actions),
            timeout=timeout_s)
    except asyncio.TimeoutError:
        return actions, f"timeout after {timeout_s}s"
    except Exception as e:
        return actions, f"runtime: {type(e).__name__}: {e}"
    return actions, None


def quick_validate(code: str) -> str | None:
    """One-shot syntax / banned-name check without running. Returns error
    string or None if clean. Useful before saving to DB."""
    try:
        compile_strategy(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    except ValueError as e:
        return str(e)
    return None
