"""Tool registry — dispatch table + OpenAI-format schema list."""
from __future__ import annotations
from . import (web_search, web_fetch, browser, rest, rag, sql, notes, market,
               ask, prefs, capability, backtest, arb, orderbook, curriculum,
               strategy)
from ..server.channels import ask_user as _ask_alias
from ..utils.log import logger

# name -> async callable
DISPATCH = {
    "web_search": web_search.web_search,
    "web_fetch": web_fetch.web_fetch,
    "browser_fetch": browser.browser_fetch,
    "rest_call": rest.rest_call,
    "rag_search": rag.rag_search,
    "rag_store": rag.rag_store,
    "sql_query": sql.sql_query,
    "note_write": notes.note_write,
    "note_list": notes.note_list,
    "read_market_ticks": market.read_market_ticks,
    "summarize_market": market.summarize_market,
    "read_aggtrades": market.read_aggtrades,
    "read_depth_diffs": market.read_depth_diffs,
    "summarize_aggtrades": market.summarize_aggtrades,
    "ask_user": ask.ask_user,
    "pref_list": prefs.pref_list,
    "pref_save": prefs.pref_save,
    "pref_delete": prefs.pref_delete,
    "pref_search": prefs.pref_search,
    "report_capability_gap": capability.report_capability_gap,
    "run_backtest": backtest.run_backtest,
    "query_arb_history": arb.query_arb_history,
    "query_orderbook_imbalance": orderbook.query_orderbook_imbalance,
    "summarize_orderbook": orderbook.summarize_orderbook,
    "curriculum_view": curriculum.curriculum_view,
    "curriculum_deep_dive": curriculum.curriculum_deep_dive,
    "design_strategy": strategy.design_strategy,
    "iterate_strategy": strategy.iterate_strategy,
    "run_strategy": strategy.run_strategy,
    "compare_strategies": strategy.compare_strategies,
    "interrupt_check": None,  # filled below
}

ALL_SCHEMAS = [
    web_search.SCHEMA,
    web_fetch.SCHEMA,
    browser.SCHEMA,
    rest.SCHEMA,
    rag.SEARCH_SCHEMA,
    rag.STORE_SCHEMA,
    sql.SCHEMA,
    notes.WRITE_SCHEMA,
    notes.LIST_SCHEMA,
    market.READ_SCHEMA,
    market.SUMMARIZE_SCHEMA,
    market.READ_AGGTRADES_SCHEMA,
    market.READ_DEPTH_DIFFS_SCHEMA,
    market.SUMMARIZE_AGGTRADES_SCHEMA,
    ask.SCHEMA,
    prefs.LIST_SCHEMA,
    prefs.SAVE_SCHEMA,
    prefs.DELETE_SCHEMA,
    prefs.SEARCH_SCHEMA,
    capability.SCHEMA,
    backtest.SCHEMA,
    arb.SCHEMA,
    orderbook.QUERY_SCHEMA,
    orderbook.SUMMARIZE_SCHEMA,
    curriculum.VIEW_SCHEMA,
    curriculum.DEEP_DIVE_SCHEMA,
    strategy.DESIGN_SCHEMA,
    strategy.ITERATE_SCHEMA,
    strategy.RUN_SCHEMA,
    strategy.COMPARE_SCHEMA,
]


async def interrupt_check() -> dict:
    from ..server.interrupt import peek_interrupt
    flag, reason = peek_interrupt()
    return {"interrupt": flag, "reason": reason}


DISPATCH["interrupt_check"] = interrupt_check
ALL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "interrupt_check",
        "description": "Check whether the user has requested an interrupt.",
        "parameters": {"type": "object", "properties": {}},
    },
})


async def call(name: str, **kwargs):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return await fn(**kwargs)
    except TypeError as e:
        return {"error": f"bad args to {name}: {e}"}
    except Exception as e:
        logger.warning(f"tool {name} raised: {e}")
        return {"error": repr(e)}
