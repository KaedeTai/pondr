"""LLM I/O logger — JSONL + in-memory ring buffer for dashboard tail."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from .. import config

_BUF: deque[dict] = deque(maxlen=200)
_LISTENERS: set[asyncio.Queue] = set()


def _sanitize(o: Any):
    try:
        json.dumps(o, default=str)
        return o
    except Exception:
        return str(o)


def log_call(kind: str, model: str, messages: list, tools: list | None,
             response: Any, function_calls: list | None,
             latency_ms: int, tokens: dict | None, trace_id: str | None = None) -> dict:
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int((time.time()%1)*1000):03d}",
        "kind": kind,
        "model": model,
        "messages": _sanitize(messages),
        "tools": _sanitize(tools) if tools else None,
        "response": _sanitize(response),
        "function_calls": _sanitize(function_calls) if function_calls else [],
        "latency_ms": latency_ms,
        "tokens": tokens or {},
        "trace_id": trace_id or str(uuid.uuid4()),
    }
    try:
        with config.LLM_LOG_PATH.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    _BUF.append(rec)
    for q in list(_LISTENERS):
        try:
            q.put_nowait(rec)
        except Exception:
            pass
    return rec


def recent(n: int = 50) -> list[dict]:
    return list(_BUF)[-n:]


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _LISTENERS.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _LISTENERS.discard(q)
