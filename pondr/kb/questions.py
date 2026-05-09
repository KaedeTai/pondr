"""SQLite persistence for ask_user pending questions.

Survives bot restarts. Each `ask_user()` call writes a row; channels query
this table on (re)connect to replay outstanding asks. Answers update the row
and resolve any in-process future awaiting it.
"""
from __future__ import annotations
import json
import time
from typing import Any
import aiosqlite
from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_questions (
    qid TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    options TEXT,
    asked_at REAL NOT NULL,
    asked_by TEXT,
    status TEXT DEFAULT 'pending',
    answer TEXT,
    answered_at REAL,
    answered_via TEXT,
    sent_to_channels TEXT,
    timeout_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pq_status ON pending_questions(status);
CREATE INDEX IF NOT EXISTS idx_pq_timeout ON pending_questions(timeout_at);
"""


async def init() -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    d = dict(row)
    try:
        d["options"] = json.loads(d["options"]) if d.get("options") else None
    except Exception:
        d["options"] = None
    try:
        d["sent_to_channels"] = json.loads(d.get("sent_to_channels") or "[]")
    except Exception:
        d["sent_to_channels"] = []
    d["age_seconds"] = max(0.0, time.time() - (d.get("asked_at") or time.time()))
    return d


async def add(qid: str, question: str, options: list[str] | None,
              asked_by: str | None, timeout_at: float | None) -> dict:
    now = time.time()
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.execute(
            """INSERT INTO pending_questions
               (qid, question, options, asked_at, asked_by, status,
                sent_to_channels, timeout_at)
               VALUES (?, ?, ?, ?, ?, 'pending', '[]', ?)""",
            (qid, question, json.dumps(options) if options else None,
             now, asked_by, timeout_at),
        )
        await db.commit()
    payload = {"qid": qid, "question": question, "options": options,
               "asked_at": now, "asked_by": asked_by, "status": "pending",
               "sent_to_channels": [], "timeout_at": timeout_at,
               "age_seconds": 0.0}
    try:
        from ..server import event_bus
        event_bus.publish("question_added", payload)
    except Exception:
        pass
    return payload


async def get(qid: str) -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pending_questions WHERE qid=?", (qid,))
        r = await cur.fetchone()
        return _row_to_dict(r) if r else None


async def list_pending() -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pending_questions WHERE status='pending' ORDER BY asked_at ASC")
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def list_recent(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM pending_questions ORDER BY asked_at DESC LIMIT ?",
            (limit,))
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def mark_sent(qid: str, channel_name: str) -> None:
    """Add channel_name to sent_to_channels JSON array if not present."""
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT sent_to_channels FROM pending_questions WHERE qid=?", (qid,))
        row = await cur.fetchone()
        if not row:
            return
        try:
            arr = json.loads(row[0] or "[]")
        except Exception:
            arr = []
        if channel_name not in arr:
            arr.append(channel_name)
            await db.execute(
                "UPDATE pending_questions SET sent_to_channels=? WHERE qid=?",
                (json.dumps(arr), qid))
            await db.commit()


async def has_been_sent_to(qid: str, channel_name: str) -> bool:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT sent_to_channels FROM pending_questions WHERE qid=?", (qid,))
        row = await cur.fetchone()
        if not row:
            return False
        try:
            return channel_name in json.loads(row[0] or "[]")
        except Exception:
            return False


async def mark_answered(qid: str, answer: str, via: str) -> bool:
    """Returns True if state moved from pending → answered."""
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT status FROM pending_questions WHERE qid=?", (qid,))
        r = await cur.fetchone()
        if not r or r[0] != "pending":
            return False
        await db.execute(
            """UPDATE pending_questions
               SET status='answered', answer=?, answered_at=?, answered_via=?
               WHERE qid=?""",
            (answer, time.time(), via, qid))
        await db.commit()
    try:
        from ..server import event_bus
        event_bus.publish("question_answered",
                          {"qid": qid, "answer": answer, "via": via})
    except Exception:
        pass
    return True


async def mark_timeout(qid: str) -> bool:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT status FROM pending_questions WHERE qid=?", (qid,))
        r = await cur.fetchone()
        if not r or r[0] != "pending":
            return False
        await db.execute(
            "UPDATE pending_questions SET status='timeout', answered_at=? WHERE qid=?",
            (time.time(), qid))
        await db.commit()
    try:
        from ..server import event_bus
        event_bus.publish("question_timed_out", {"qid": qid})
    except Exception:
        pass
    return True


async def list_expired(now: float | None = None) -> list[dict]:
    """Pending rows with timeout_at <= now."""
    now = now if now is not None else time.time()
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pending_questions
               WHERE status='pending' AND timeout_at IS NOT NULL AND timeout_at <= ?""",
            (now,))
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def cancel(qid: str) -> bool:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "UPDATE pending_questions SET status='cancelled' WHERE qid=? AND status='pending'",
            (qid,))
        await db.commit()
        return cur.rowcount > 0
