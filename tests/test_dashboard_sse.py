"""Event bus + /api/stream SSE smoke tests."""
import asyncio
import json

import pytest

from pondr.server import event_bus
from pondr.server import dashboard


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_event_bus_publish_subscribe():
    event_bus.clear()
    q = event_bus.subscribe()
    rec = event_bus.publish("task_added", {"id": 1, "topic": "test"})
    assert rec["type"] == "task_added"
    assert rec["payload"] == {"id": 1, "topic": "test"}
    got = aio(asyncio.wait_for(q.get(), timeout=1.0))
    assert got is rec or (got["type"] == "task_added" and got["payload"]["id"] == 1)
    event_bus.unsubscribe(q)


def test_event_bus_multiple_subscribers():
    event_bus.clear()
    q1 = event_bus.subscribe()
    q2 = event_bus.subscribe()
    event_bus.publish("finding_added", {"id": 7})
    g1 = aio(asyncio.wait_for(q1.get(), timeout=1.0))
    g2 = aio(asyncio.wait_for(q2.get(), timeout=1.0))
    assert g1["type"] == g2["type"] == "finding_added"
    event_bus.unsubscribe(q1)
    event_bus.unsubscribe(q2)


def test_event_bus_recent_buffer():
    event_bus.clear()
    for i in range(5):
        event_bus.publish("task_added", {"id": i})
    recent = event_bus.recent(3)
    assert len(recent) == 3
    assert [r["payload"]["id"] for r in recent] == [2, 3, 4]


def test_event_bus_full_listener_queue_does_not_raise():
    event_bus.clear()
    q = event_bus.subscribe(maxsize=2)
    # Fill the queue so the next publish would QueueFull.
    event_bus.publish("task_added", {"id": 1})
    event_bus.publish("task_added", {"id": 2})
    # This should NOT raise even though the listener's queue is full.
    rec = event_bus.publish("task_added", {"id": 3})
    assert rec["payload"]["id"] == 3
    event_bus.unsubscribe(q)


def test_sse_route_registered():
    """The /api/stream endpoint should be wired into the FastAPI app."""
    paths = [getattr(r, "path", None) for r in dashboard.app.routes]
    assert "/api/stream" in paths


def test_event_bus_known_event_types_documented():
    """Catch typos in producer call sites — the type should be in
    KNOWN_EVENT_TYPES (not enforced at runtime, just a doc check)."""
    expected = {
        "tick_count_update", "task_added", "task_update", "finding_added",
        "llm_io_logged", "question_added", "question_answered",
        "question_timed_out", "capability_gap_added", "capability_gap_updated",
        "preference_changed", "curriculum_updated", "arb_opportunity",
        "orderbook_alert",
    }
    assert expected.issubset(event_bus.KNOWN_EVENT_TYPES)
