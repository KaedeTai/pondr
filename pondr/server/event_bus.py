"""In-memory pub/sub event bus for dashboard SSE.

Modelled on :mod:`pondr.utils.llm_log` (a simple set of asyncio.Queues).
Producers call :func:`publish`; the dashboard SSE endpoint subscribes
through :func:`subscribe` and gets each event in order.

All publishes are best-effort: if a listener queue is full or has been
garbage-collected, the event is dropped for that listener — never raised
back to the caller — so wiring publish() into business logic cannot break
that logic.
"""
from __future__ import annotations
import asyncio
import time
import uuid
from collections import deque
from typing import Any

# Event types used across the system. Listed here for discoverability;
# publish() does NOT validate against this set, since adding a new event
# type should be a one-line change at the call site.
KNOWN_EVENT_TYPES = frozenset({
    "tick_count_update",
    "task_added", "task_update",
    "finding_added",
    "llm_io_logged",
    "question_added", "question_answered", "question_timed_out",
    "capability_gap_added", "capability_gap_updated",
    "preference_changed",
    "curriculum_updated",
    "arb_opportunity",
    "orderbook_alert",
    "llm_stats_updated",
})

_BUF: deque[dict] = deque(maxlen=500)
_LISTENERS: set[asyncio.Queue] = set()


def publish(event_type: str, payload: dict | None = None) -> dict:
    """Append-only fanout. Never raises."""
    rec = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "type": event_type,
        "payload": payload or {},
    }
    try:
        _BUF.append(rec)
    except Exception:
        pass
    for q in list(_LISTENERS):
        try:
            q.put_nowait(rec)
        except asyncio.QueueFull:
            # Listener is too slow — drop this event for that listener.
            pass
        except Exception:
            pass
    return rec


def subscribe(maxsize: int = 500) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    _LISTENERS.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _LISTENERS.discard(q)


def recent(n: int = 100) -> list[dict]:
    """Return the n most recent events for replay-on-connect."""
    return list(_BUF)[-n:]


def clear() -> None:
    """Test helper — wipe buffer + listeners."""
    _BUF.clear()
    _LISTENERS.clear()
