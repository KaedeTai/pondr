"""run_strategy: end-to-end with synthetic ticks via monkeypatched duckdb."""
import asyncio

import pytest

from pondr.kb import strategies as strat_kb
from pondr.tools import strategy as strat_tool


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


SAMPLE_BUY_AND_HOLD = (
    "def on_tick(state, tick):\n"
    "    if not state:\n"
    "        state['done'] = False\n"
    "    if not state['done']:\n"
    "        state['done'] = True\n"
    "        return {'side':'buy','qty':0.1,'reason':'init'}\n"
    "    return {'side':'hold','qty':0,'reason':'wait'}\n"
)


def _make_ticks(n=200, base=100.0, drift=1.0):
    return [{"ts": float(i), "source": "test", "symbol": "SYN",
             "price": base + drift * i, "qty": 0.0, "side": ""}
            for i in range(n)]


def test_run_strategy_drives_engine(monkeypatch):
    sid = aio(strat_kb.add(
        name="hold_test", hypothesis="hold long for drift",
        code_python=SAMPLE_BUY_AND_HOLD))
    from pondr.kb import duckdb as ddb

    async def fake_query(_sql):
        return _make_ticks(n=200, drift=1.0)
    monkeypatch.setattr(ddb, "query", fake_query)

    out = aio(strat_tool.run_strategy(sid, symbol="SYN"))
    assert "metrics" in out
    assert out["n_ticks"] == 200
    m = out["metrics"]
    # buy at price 100, force-close at price 299 → positive realized
    assert m["final_pnl"] > 0
    assert m["n_trades"] == 2  # buy + auto-close


def test_run_strategy_persists_with_strategy_id(monkeypatch):
    sid = aio(strat_kb.add(
        name="persist_test", hypothesis="persist test",
        code_python=SAMPLE_BUY_AND_HOLD))
    from pondr.kb import duckdb as ddb

    async def fake_query(_sql):
        return _make_ticks(n=50, drift=0.5)
    monkeypatch.setattr(ddb, "query", fake_query)

    out = aio(strat_tool.run_strategy(sid, symbol="SYN"))
    bt = aio(strat_kb.last_backtest(sid))
    assert bt is not None
    assert bt["id"] == out["backtest_id"]


def test_run_strategy_rejects_compile_error(monkeypatch):
    sid = aio(strat_kb.add(
        name="broken", hypothesis="will not compile",
        code_python="import os\ndef on_tick(s,t): return {'side':'hold'}",
        status="compile_error"))
    out = aio(strat_tool.run_strategy(sid, symbol="SYN"))
    assert "error" in out
    assert "compile_error" in out["error"]


def test_run_strategy_handles_no_ticks(monkeypatch):
    sid = aio(strat_kb.add(
        name="no_data", hypothesis="empty",
        code_python=SAMPLE_BUY_AND_HOLD))
    from pondr.kb import duckdb as ddb

    async def fake_query(_sql):
        return []
    monkeypatch.setattr(ddb, "query", fake_query)
    out = aio(strat_tool.run_strategy(sid, symbol="ZZZ"))
    assert "error" in out
    assert "no ticks" in out["error"]


def test_run_strategy_unknown_id():
    out = aio(strat_tool.run_strategy(999_999, symbol="X"))
    assert "error" in out
