"""Confidence scoring tests — synthesizer attaches conf to findings + triangulation auto-task."""
import asyncio
import uuid
import pytest
from unittest.mock import patch, AsyncMock
from pondr.kb import sqlite as kb_sql
from pondr.research import synthesizer as synth


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def test_notes_table_has_confidence_columns():
    """Migration must have added the columns."""
    import sqlite3
    from pondr import config
    con = sqlite3.connect(config.DB_KB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(notes)")}
    con.close()
    assert {"confidence", "confidence_reason", "source_count"}.issubset(cols)


def test_add_note_with_confidence():
    nid = aio(kb_sql.add_note(f"t/{uuid.uuid4().hex[:6]}", "test content",
                               confidence=0.8, confidence_reason="2 sources",
                               source_count=2))
    assert nid > 0
    rows = aio(kb_sql.list_notes("t/"))
    me = next(r for r in rows if r["id"] == nid)
    assert me["confidence"] == 0.8 and me["source_count"] == 2


def test_update_confidence_after_triangulation():
    nid = aio(kb_sql.add_note(f"t/u-{uuid.uuid4().hex[:6]}", "claim X",
                               confidence=0.4, source_count=1))
    aio(kb_sql.update_note_confidence(nid, 0.85,
                                       reason="triangulated by 2 sources",
                                       source_count=3))
    rows = aio(kb_sql.find_notes_by_topic("t/u-"))
    me = next(r for r in rows if r["id"] == nid)
    assert me["confidence"] == 0.85 and me["source_count"] == 3


def test_low_confidence_enqueues_triangulation():
    """Mock the LLM to return a low-confidence finding and verify auto-task."""
    fake_synth = {"choices": [{"message": {
        "content": '{"finding":"X is true","sources":["src1"],"conflicts":[],"source_count":1}'}}]}
    fake_score = {"choices": [{"message": {
        "content": '{"confidence":0.3,"reason":"single source"}'}}]}

    pre = aio(kb_sql.list_tasks(status="queued", limit=200))
    pre_count = len([t for t in pre if t["topic"].startswith("[triangulate]")])

    with patch("pondr.research.synthesizer.llm.chat",
               new=AsyncMock(side_effect=[fake_synth, fake_score])):
        out = aio(synth.synthesize(
            f"unique-topic-{uuid.uuid4().hex[:6]}",
            [{"title": "sub1", "answer": "some content"}]))

    assert out["confidence"] == 0.3
    post = aio(kb_sql.list_tasks(status="queued", limit=200))
    post_count = len([t for t in post if t["topic"].startswith("[triangulate]")])
    assert post_count == pre_count + 1


def test_high_confidence_skips_triangulation():
    fake_synth = {"choices": [{"message": {
        "content": '{"finding":"Y","sources":["a","b"],"conflicts":[],"source_count":2}'}}]}
    fake_score = {"choices": [{"message": {
        "content": '{"confidence":0.9,"reason":"two reputable sources"}'}}]}

    pre = aio(kb_sql.list_tasks(status="queued", limit=200))
    pre_count = len([t for t in pre if t["topic"].startswith("[triangulate]")])

    with patch("pondr.research.synthesizer.llm.chat",
               new=AsyncMock(side_effect=[fake_synth, fake_score])):
        out = aio(synth.synthesize(
            f"unique-topic-hi-{uuid.uuid4().hex[:6]}",
            [{"title": "sub1", "answer": "some content"}]))

    assert out["confidence"] == 0.9
    post = aio(kb_sql.list_tasks(status="queued", limit=200))
    post_count = len([t for t in post if t["topic"].startswith("[triangulate]")])
    assert post_count == pre_count  # unchanged
