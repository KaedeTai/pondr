"""Chat router 'answer' action runs a second LLM call with KB context.

We do not exercise the WS server here; we call the helper that the dispatch
loop uses (``_build_chat_answer``) directly with a stubbed llm.chat to verify:

  * the second call happens
  * the prompt contains RAG / curriculum / recent-findings sections
  * the function returns the assistant text
"""
import asyncio
import sys
from pondr import __main__ as pondr_main
from pondr import llm


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_build_chat_answer_invokes_second_llm_with_context(monkeypatch):
    captured: dict = {}

    async def fake_chat(messages, **kw):
        captured["messages"] = messages
        captured["kw"] = kw
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "ETH funding rate stub reply."}}]}

    async def fake_search(q, k=5):
        return [{"id": "abc12345", "doc": "ETH perp funding 0.01%/8h",
                 "meta": {}, "distance": 0.1}]

    async def fake_tree():
        return [{"title": "Funding rates", "status": "medium",
                 "mastery_pct": 40.0, "children": []}]

    async def fake_find(prefix, limit=10):
        return [{"topic": "finding/eth-funding",
                 "content": "ETH funding currently positive",
                 "confidence": 0.7}]

    monkeypatch.setattr(llm, "chat", fake_chat)
    from pondr.kb import chroma as chroma_kb
    from pondr.kb import curriculum as curr_kb
    from pondr.kb import sqlite as kb_sql
    monkeypatch.setattr(chroma_kb, "search", fake_search)
    monkeypatch.setattr(curr_kb, "tree", fake_tree)
    monkeypatch.setattr(kb_sql, "find_notes_by_topic", fake_find)

    reply = aio(pondr_main._build_chat_answer(
        "那 ETH funding rate 你目前知道什麼?"))

    assert reply == "ETH funding rate stub reply."
    msgs = captured["messages"]
    assert msgs[0]["role"] == "system"
    assert "pondr" in msgs[0]["content"].lower()
    user_blob = msgs[1]["content"]
    assert "Relevant KB chunks (RAG)" in user_blob
    assert "ETH perp funding" in user_blob
    assert "Current curriculum" in user_blob
    assert "Funding rates" in user_blob
    assert "Recent findings" in user_blob
    assert "ETH funding currently positive" in user_blob
    # second call should be the answer call (lower temperature, tighter budget
    # — reduced from 800 to 400 as part of context-truncation work)
    assert captured["kw"]["temperature"] == 0.3
    assert captured["kw"]["max_tokens"] == 400


def test_build_chat_answer_falls_back_on_kb_failure(monkeypatch):
    """If KB lookups blow up, we should still attempt an LLM call and not raise."""

    async def fake_chat(messages, **kw):
        return {"choices": [{"message": {"role": "assistant",
                                         "content": "fallback reply"}}]}

    async def boom(*a, **kw):
        raise RuntimeError("kb down")

    monkeypatch.setattr(llm, "chat", fake_chat)
    from pondr.kb import chroma as chroma_kb
    from pondr.kb import curriculum as curr_kb
    from pondr.kb import sqlite as kb_sql
    monkeypatch.setattr(chroma_kb, "search", boom)
    monkeypatch.setattr(curr_kb, "tree", boom)
    monkeypatch.setattr(kb_sql, "find_notes_by_topic", boom)

    reply = aio(pondr_main._build_chat_answer("hi"))
    assert reply == "fallback reply"
