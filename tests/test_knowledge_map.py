"""Knowledge gap map tests — reflection enqueues sub-tasks for unknowns."""
import asyncio
import uuid
from unittest.mock import patch, AsyncMock
from pondr.kb import knowledge_gaps as kg_kb, sqlite as kb_sql
from pondr.research import knowledge_map as kmap


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def test_upsert_and_list():
    topic = f"t-{uuid.uuid4().hex[:6]}"
    aio(kg_kb.upsert(topic, "what is X?", "unknown"))
    aio(kg_kb.upsert(topic, "what is X?", "known", answer_summary="X is Y"))
    rows = aio(kg_kb.list_by_topic(topic=topic))
    assert len(rows) == 1 and rows[0]["status"] == "known"
    assert rows[0]["answer_summary"] == "X is Y"


def test_tree_groups_by_topic():
    t1 = f"a-{uuid.uuid4().hex[:6]}"
    t2 = f"b-{uuid.uuid4().hex[:6]}"
    aio(kg_kb.upsert(t1, "q1"))
    aio(kg_kb.upsert(t2, "q2"))
    tree = aio(kg_kb.tree())
    assert t1 in tree and t2 in tree


def test_mark_status_transition():
    topic = f"t-st-{uuid.uuid4().hex[:6]}"
    gid = aio(kg_kb.upsert(topic, "evolves?"))
    assert aio(kg_kb.mark_status(gid, "researching"))
    assert aio(kg_kb.mark_status(gid, "known", answer_summary="yes"))
    rows = aio(kg_kb.list_by_topic(topic=topic))
    assert rows[0]["status"] == "known"


def test_reflect_enqueues_unknown_subtasks():
    topic = f"reflect-{uuid.uuid4().hex[:6]}"
    fake = {"choices": [{"message": {"content": (
        '{"known":[{"q":"what is sharpe?","a":"return/std"}],'
        '"researching":["how to size positions"],'
        '"unknown":["how to handle fat tails","what is optimal leverage"]}'
    )}}]}
    pre = aio(kb_sql.list_tasks(limit=200))
    pre_count = len([t for t in pre if t["topic"].startswith(f"[gap] {topic}")])

    with patch("pondr.research.knowledge_map.llm.chat",
               new=AsyncMock(return_value=fake)):
        counts = aio(kmap.reflect_on_topic(topic))

    assert counts["known"] == 1
    assert counts["researching"] == 1
    assert counts["unknown"] == 2
    assert counts["enqueued"] == 2
    rows = aio(kg_kb.list_by_topic(topic=topic))
    assert {r["status"] for r in rows} == {"known", "researching", "unknown"}
    post = aio(kb_sql.list_tasks(limit=200))
    post_count = len([t for t in post if t["topic"].startswith(f"[gap] {topic}")])
    assert post_count == pre_count + 2


def test_counts_by_status():
    counts = aio(kg_kb.counts_by_status())
    assert isinstance(counts, dict)


def test_reflect_depth_cap_skips_grandchild_enqueue():
    """A reflection on an already-[gap] parent must NOT enqueue more [gap]s.

    Otherwise self-reflection turns into runaway recursion
    ([gap] [gap] [gap] ... :: ...).
    """
    base = f"reflect-{uuid.uuid4().hex[:6]}"
    parent_topic = f"[gap] {base} :: how to handle fat tails"
    fake = {"choices": [{"message": {"content": (
        '{"known":[],"researching":[],'
        '"unknown":["sub-question A","sub-question B"]}'
    )}}]}

    pre = aio(kb_sql.list_tasks(limit=400))
    pre_count = len([t for t in pre
                     if t["topic"].startswith(f"[gap] {parent_topic}")])

    with patch("pondr.research.knowledge_map.llm.chat",
               new=AsyncMock(return_value=fake)):
        counts = aio(kmap.reflect_on_topic(parent_topic))

    # KB rows still get upserted (so the dashboard can show them) but
    # nothing new is enqueued at depth >= MAX_GAP_DEPTH.
    assert counts["unknown"] == 2
    assert counts["enqueued"] == 0
    post = aio(kb_sql.list_tasks(limit=400))
    post_count = len([t for t in post
                      if t["topic"].startswith(f"[gap] {parent_topic}")])
    assert post_count == pre_count, (
        f"depth cap failed: {post_count - pre_count} new sub-tasks enqueued"
    )


def test_reflect_backtest_cap_skips_when_parent_is_gap():
    """Backtest auto-enqueue should also respect depth cap.

    A finding in a depth-1 [gap] task whose sub-question contains a
    backtest trigger keyword must not enqueue a [backtest] task — that's
    how we ended up with stacks of derivative backtests.
    """
    base = f"reflect-{uuid.uuid4().hex[:6]}"
    parent_topic = f"[gap] {base}"
    fake = {"choices": [{"message": {"content": (
        '{"known":[],"researching":[],'
        '"unknown":["BTC momentum strategy sharpe ratio?"]}'
    )}}]}

    pre = aio(kb_sql.list_tasks(limit=400))
    pre_bt = len([t for t in pre if t["topic"].startswith("[backtest]")])

    with patch("pondr.research.knowledge_map.llm.chat",
               new=AsyncMock(return_value=fake)):
        aio(kmap.reflect_on_topic(parent_topic))

    post = aio(kb_sql.list_tasks(limit=400))
    post_bt = len([t for t in post if t["topic"].startswith("[backtest]")])
    assert post_bt == pre_bt, (
        f"backtest cap failed: {post_bt - pre_bt} new [backtest] tasks enqueued"
    )
