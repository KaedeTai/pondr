"""Tool smoke tests — fast, no network where possible."""
import asyncio
import pytest
from pondr.tools import notes, sql, market, ask
from pondr.kb import sqlite as kb_sql


def aio(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_kb_init():
    aio(kb_sql.init())
    aio(kb_sql.add_note("test/topic", "hello"))
    rows = aio(kb_sql.list_notes("test/"))
    assert any(r["content"] == "hello" for r in rows)


def test_note_tool():
    aio(notes.note_write("unit/test", "via tool"))
    rs = aio(notes.note_list("unit/"))
    assert any(r["content"] == "via tool" for r in rs)


def test_sql_select_only():
    out = aio(sql.sql_query("kb", "DELETE FROM notes"))
    assert "error" in out


def test_market_summary_empty():
    import pytest, _duckdb
    try:
        s = aio(market.summarize_market("DOES_NOT_EXIST"))
    except _duckdb.IOException:
        pytest.skip("market_ticks.db is locked (bot running)")
    assert s["count"] == 0


def test_ask_user_schema():
    assert ask.SCHEMA["function"]["name"] == "ask_user"
    assert "question" in ask.SCHEMA["function"]["parameters"]["properties"]
