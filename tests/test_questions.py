"""Persistent pending-question tests."""
import asyncio
import time
import uuid
from pondr.kb import questions as q_kb


def aio(c): return asyncio.get_event_loop().run_until_complete(c)
def uid(prefix): return f"test-{prefix}-{uuid.uuid4().hex[:8]}"


def test_add_and_list_pending():
    qid = uid("add")
    pre = aio(q_kb.list_pending())
    aio(q_kb.add(qid, "test question?", ["yes", "no"], "task#999", None))
    cur = aio(q_kb.list_pending())
    assert any(r["qid"] == qid for r in cur)
    assert len(cur) == len(pre) + 1


def test_mark_sent_and_idempotent():
    qid = uid("sent")
    aio(q_kb.add(qid, "another?", None, None, None))
    aio(q_kb.mark_sent(qid, "telegram"))
    aio(q_kb.mark_sent(qid, "telegram"))
    assert aio(q_kb.has_been_sent_to(qid, "telegram"))
    assert not aio(q_kb.has_been_sent_to(qid, "stdio"))


def test_mark_answered_state_transition():
    qid = uid("ans")
    aio(q_kb.add(qid, "?", None, None, None))
    assert aio(q_kb.mark_answered(qid, "yes", "ws"))
    assert not aio(q_kb.mark_answered(qid, "no", "stdio"))
    row = aio(q_kb.get(qid))
    assert row["status"] == "answered" and row["answer"] == "yes"


def test_timeout_sweep():
    qid = uid("timeout")
    aio(q_kb.add(qid, "expire?", None, None, time.time() - 5))
    expired = aio(q_kb.list_expired())
    assert any(r["qid"] == qid for r in expired)
    assert aio(q_kb.mark_timeout(qid))
    assert aio(q_kb.get(qid))["status"] == "timeout"


def test_resolve_question_broadcasts():
    from pondr.server.channels.base import resolve_question
    qid = uid("resolve")
    aio(q_kb.add(qid, "q5?", None, None, None))
    moved = aio(resolve_question(qid, "answer-text", via="test"))
    assert moved is True
    assert not aio(resolve_question(qid, "twice", via="test"))
