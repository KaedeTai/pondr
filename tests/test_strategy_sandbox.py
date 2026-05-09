"""Sandbox: malicious code rejected, well-formed code runs and times out."""
import asyncio
import pytest

from pondr.quant.strategies.sandbox import (
    compile_strategy, quick_validate, run_strategy_safe)


def aio(c): return asyncio.new_event_loop().run_until_complete(c)


def test_rejects_import():
    with pytest.raises(ValueError):
        compile_strategy("import os\ndef on_tick(s,t): return {'side':'hold'}")


def test_rejects_from_import():
    with pytest.raises(ValueError):
        compile_strategy("from os import system\ndef on_tick(s,t): return {}")


def test_rejects_open():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    open('/etc/passwd').read()\n    return {}")


def test_rejects_eval():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    eval('1+1')\n    return {}")


def test_rejects_exec():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    exec('x=1')\n    return {}")


def test_rejects_dunder_attr():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    return ().__class__.__bases__[0]\n")


def test_rejects_getattr():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    getattr(s, 'x', 0)\n    return {}")


def test_rejects_compile_call():
    with pytest.raises(ValueError):
        compile_strategy(
            "def on_tick(s,t):\n    compile('x','f','exec')\n    return {}")


def test_rejects_oversized_code():
    big = "def on_tick(s,t):\n    return {'side':'hold'}\n" + ("# x\n" * 5000)
    with pytest.raises(ValueError):
        compile_strategy(big)


def test_rejects_missing_on_tick():
    with pytest.raises(ValueError):
        compile_strategy("def something_else(s,t): return {}")


def test_accepts_simple_strategy():
    code = (
        "def on_tick(state, tick):\n"
        "    if 'n' not in state:\n"
        "        state['n'] = 0\n"
        "    state['n'] += 1\n"
        "    return {'side': 'hold', 'qty': 0, 'reason': 'ok'}\n"
    )
    fn = compile_strategy(code)
    assert callable(fn)
    state = {}
    out = fn(state, {"price": 100, "ts": 1, "side": "buy", "qty": 1})
    assert out["side"] == "hold"
    assert state["n"] == 1


def test_quick_validate_returns_string_on_error():
    err = quick_validate("import os")
    assert err is not None
    assert "import" in err
    assert quick_validate("def on_tick(s,t): return {'side':'hold'}") is None


def test_run_strategy_safe_returns_actions():
    code = (
        "def on_tick(state, tick):\n"
        "    return {'side': 'buy' if tick['price'] > 100 else 'hold',\n"
        "            'qty': 0.01, 'reason': 'r'}\n"
    )
    ticks = [{"price": p, "ts": float(i), "symbol": "X",
              "side": "", "qty": 0, "exchange": ""}
             for i, p in enumerate([90, 95, 105, 110, 120])]
    actions, err = aio(run_strategy_safe(code, ticks, timeout_s=2.0))
    assert err is None
    assert len(actions) == len(ticks)
    assert sum(1 for a in actions if a["side"] == "buy") == 3


def test_run_strategy_safe_timeout_between_ticks():
    """Timeout enforced between ticks — many fast ticks let asyncio cancel
    the loop. Note: the sandbox cannot interrupt a sync `while True: pass`
    *inside* one tick (CPython doesn't yield to the event loop), so the
    realistic protection is per-tick aggregate, not per-statement."""
    # Each on_tick call sleeps a tiny bit by doing real work; with 50k ticks
    # at yield_every=50 we'll cooperatively yield ~1000 times, well within
    # the 0.3s budget the test gives the loop.
    code = (
        "def on_tick(state, tick):\n"
        "    state.setdefault('s', 0)\n"
        "    # Do enough work per tick that 50k ticks blow past the budget\n"
        "    for i in range(2000):\n"
        "        state['s'] += i\n"
        "    return {'side':'hold','qty':0,'reason':'busy'}\n"
    )
    ticks = [{"price": 100.0, "ts": float(i), "symbol": "X",
              "side": "", "qty": 0, "exchange": ""}
             for i in range(50_000)]
    actions, err = aio(run_strategy_safe(code, ticks, timeout_s=0.3))
    assert err is not None
    assert "timeout" in err.lower()
    # Some actions should have been collected before the timeout
    assert 0 < len(actions) < len(ticks)


def test_runtime_error_records_noop():
    """A strategy raising at runtime gets a noop placeholder, not a crash."""
    code = (
        "def on_tick(state, tick):\n"
        "    return tick['price'] / 0\n"  # ZeroDivisionError, also wrong type
    )
    ticks = [{"price": 100.0, "ts": 0.0, "symbol": "X",
              "side": "", "qty": 0, "exchange": ""}]
    actions, err = aio(run_strategy_safe(code, ticks, timeout_s=2.0))
    assert err is None  # the loop swallows per-tick errors
    assert actions[0]["side"] == "noop"
    assert actions[0].get("error") is True


def test_safe_builtins_available():
    code = (
        "def on_tick(state, tick):\n"
        "    state.setdefault('p', [])\n"
        "    state['p'].append(tick['price'])\n"
        "    avg = sum(state['p']) / len(state['p'])\n"
        "    mx = max(state['p'])\n"
        "    return {'side':'hold','qty':0,'reason':f'{avg:.2f} max={mx}'}\n"
    )
    ticks = [{"price": float(p), "ts": float(i), "symbol": "X",
              "side":"", "qty":0, "exchange":""} for i, p in enumerate([1,2,3])]
    actions, err = aio(run_strategy_safe(code, ticks))
    assert err is None
    assert "avg" not in actions[0]["reason"]  # the f-string was substituted
    assert "max=" in actions[-1]["reason"]


def test_math_module_available():
    code = (
        "def on_tick(state, tick):\n"
        "    return {'side':'hold','qty':0,'reason':str(math.sqrt(tick['price']))}\n"
    )
    ticks = [{"price": 16.0, "ts": 0.0, "symbol":"X","side":"","qty":0,"exchange":""}]
    actions, err = aio(run_strategy_safe(code, ticks))
    assert err is None
    assert actions[0]["reason"] == "4.0"


def test_np_limited_namespace():
    code = (
        "def on_tick(state, tick):\n"
        "    state.setdefault('p', [])\n"
        "    state['p'].append(tick['price'])\n"
        "    arr = np.array(state['p'])\n"
        "    m = float(np.mean(arr))\n"
        "    return {'side':'hold','qty':0,'reason':f'mean={m:.2f}'}\n"
    )
    ticks = [{"price": float(p), "ts":float(i),"symbol":"X","side":"","qty":0,"exchange":""}
             for i,p in enumerate([1,2,3,4,5])]
    actions, err = aio(run_strategy_safe(code, ticks))
    assert err is None
    assert "mean=3.00" in actions[-1]["reason"]


def test_np_dangerous_attrs_unavailable():
    """np.load / np.fromfile etc. must not be present."""
    code = (
        "def on_tick(state, tick):\n"
        "    np.load('/etc/passwd')\n"
        "    return {'side':'hold'}\n"
    )
    # compile_strategy should accept (np is a name); failure happens at runtime
    fn = compile_strategy(code)
    state = {}
    with pytest.raises(AttributeError):
        fn(state, {"price": 1.0, "ts": 0, "side":"", "qty":0,
                   "symbol":"X", "exchange":""})
