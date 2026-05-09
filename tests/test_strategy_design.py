"""design_strategy / iterate_strategy / run_strategy / compare_strategies."""
import asyncio
import json

import pytest

from pondr.kb import strategies as strat_kb
from pondr.tools import strategy as strat_tool


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


SAMPLE_GOOD_CODE = (
    "def on_tick(state, tick):\n"
    "    if 'prices' not in state:\n"
    "        state['prices'] = []\n"
    "        state['pos'] = 0\n"
    "    state['prices'].append(tick['price'])\n"
    "    if len(state['prices']) < 10:\n"
    "        return {'side':'hold','qty':0,'reason':'warm'}\n"
    "    short_ma = sum(state['prices'][-3:]) / 3\n"
    "    long_ma = sum(state['prices'][-10:]) / 10\n"
    "    if short_ma > long_ma and state['pos'] <= 0:\n"
    "        state['pos'] = 1\n"
    "        return {'side':'buy','qty':0.01,'reason':'cross up'}\n"
    "    if short_ma < long_ma and state['pos'] >= 0:\n"
    "        state['pos'] = -1\n"
    "        return {'side':'sell','qty':0.01,'reason':'cross dn'}\n"
    "    return {'side':'hold','qty':0,'reason':'no signal'}\n"
)


class _FakeLLMResp:
    """Stub for llm.chat — returns predetermined strategy code."""
    def __init__(self, code: str):
        self._code = code

    def __call__(self, *args, **kwargs):
        async def _r():
            return {
                "choices": [{"message": {"content": self._code,
                                          "role": "assistant"}}],
            }
        return _r()


def test_design_strategy_persists_with_lineage(monkeypatch):
    from pondr import llm
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(SAMPLE_GOOD_CODE))
    out = aio(strat_tool.design_strategy(
        name="ma_test", hypothesis="short MA crossing long MA predicts trend"))
    assert "strategy_id" in out
    assert out["status"] == "ok"
    sid = out["strategy_id"]
    fetched = aio(strat_kb.get(sid))
    assert fetched is not None
    assert fetched["name"] == "ma_test"
    assert fetched["lineage_parent_id"] is None
    assert "on_tick" in fetched["code_python"]


def test_design_strategy_marks_compile_error(monkeypatch):
    from pondr import llm
    bad = "import os\ndef on_tick(s,t): return {'side':'hold'}"
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(bad))
    out = aio(strat_tool.design_strategy(
        name="bad_test", hypothesis="this will compile-error"))
    assert "validation_error" in out
    assert out["status"] == "compile_error"
    sid = out["strategy_id"]
    fetched = aio(strat_kb.get(sid))
    assert fetched["status"] == "compile_error"


def test_design_strategy_strips_code_fences(monkeypatch):
    from pondr import llm
    fenced = "```python\n" + SAMPLE_GOOD_CODE + "\n```"
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(fenced))
    out = aio(strat_tool.design_strategy(
        name="fenced", hypothesis="LLM put fences around its output"))
    assert out["status"] == "ok"
    assert "```" not in out["code"]


def test_iterate_strategy_creates_child(monkeypatch):
    from pondr import llm
    # First design a parent
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(SAMPLE_GOOD_CODE))
    parent = aio(strat_tool.design_strategy(
        name="parent", hypothesis="parent strategy"))
    # Now iterate with a different code body
    child_code = SAMPLE_GOOD_CODE.replace("len(state['prices']) < 10",
                                          "len(state['prices']) < 50")
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(child_code))
    out = aio(strat_tool.iterate_strategy(
        parent["strategy_id"],
        "use a longer warmup window"))
    assert out["status"] == "ok"
    child = aio(strat_kb.get(out["strategy_id"]))
    assert child["lineage_parent_id"] == parent["strategy_id"]
    assert "len(state['prices']) < 50" in child["code_python"]


def test_lineage_tree_walks_descendants(monkeypatch):
    from pondr import llm
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(SAMPLE_GOOD_CODE))
    p = aio(strat_tool.design_strategy(
        name="root_x", hypothesis="root for tree test"))
    p_id = p["strategy_id"]
    # iterate twice off the parent
    c1 = aio(strat_tool.iterate_strategy(p_id, "tweak A"))
    c2 = aio(strat_tool.iterate_strategy(p_id, "tweak B"))
    # iterate once off c1
    aio(strat_tool.iterate_strategy(c1["strategy_id"], "tweak A.1"))
    tree = aio(strat_kb.lineage_tree(p_id))
    assert tree["id"] == p_id
    child_ids = sorted(c["id"] for c in tree["children"])
    assert child_ids == sorted([c1["strategy_id"], c2["strategy_id"]])
    # c1 should have a grandchild
    c1_subtree = next(c for c in tree["children"] if c["id"] == c1["strategy_id"])
    assert len(c1_subtree["children"]) == 1


def test_compare_strategies_handles_missing_backtests(monkeypatch):
    from pondr import llm
    monkeypatch.setattr(llm, "chat", _FakeLLMResp(SAMPLE_GOOD_CODE))
    a = aio(strat_tool.design_strategy(name="cmp_a", hypothesis="aaa"))
    b = aio(strat_tool.design_strategy(name="cmp_b", hypothesis="bbb"))
    out = aio(strat_tool.compare_strategies(
        [a["strategy_id"], b["strategy_id"], 99999]))
    assert out["n"] == 3
    # 2 with 'no backtest yet', 1 with 'not found'
    msgs = [r.get("error", "") for r in out["comparison"]]
    assert sum("no backtest" in m for m in msgs) == 2
    assert sum("not found" in m for m in msgs) == 1
