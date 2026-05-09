"""Capability-gap registry — bot self-reports missing capabilities.

Re-reports of the same capability bump report_count (debounce, not spam).
High severity (4-5) triggers an ask_user immediately via the tool layer.
"""
from __future__ import annotations
import time
import aiosqlite
from .. import config


def _publish(event_type: str, payload: dict) -> None:
    try:
        from ..server import event_bus
        event_bus.publish(event_type, payload)
    except Exception:
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS capability_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capability TEXT NOT NULL UNIQUE,
    why_needed TEXT,
    severity INTEGER DEFAULT 3,
    suggested_solution TEXT,
    first_reported_at REAL,
    last_reported_at REAL,
    report_count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open',
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_capgap_status ON capability_gaps(status, severity DESC);
"""


async def init() -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _row(r: aiosqlite.Row) -> dict:
    return dict(r)


async def report(capability: str, why_needed: str, severity: int = 3,
                 suggested_solution: str | None = None) -> dict:
    """Insert or bump report_count. Returns the row + 'created' flag + 'is_high'."""
    capability = (capability or "").strip()
    if not capability:
        return {"ok": False, "error": "capability required"}
    severity = max(1, min(5, int(severity or 3)))
    now = time.time()
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT id, report_count, severity, status FROM capability_gaps WHERE capability=?",
            (capability,))
        row = await cur.fetchone()
        if row:
            new_sev = max(int(row[2]), severity)
            await db.execute(
                """UPDATE capability_gaps
                   SET why_needed=COALESCE(?, why_needed),
                       severity=?,
                       suggested_solution=COALESCE(?, suggested_solution),
                       last_reported_at=?,
                       report_count=report_count+1
                   WHERE id=?""",
                (why_needed, new_sev, suggested_solution, now, row[0]))
            await db.commit()
            cap_id = row[0]
            created = False
        else:
            cur = await db.execute(
                """INSERT INTO capability_gaps
                   (capability, why_needed, severity, suggested_solution,
                    first_reported_at, last_reported_at, report_count, status)
                   VALUES (?, ?, ?, ?, ?, ?, 1, 'open')""",
                (capability, why_needed, severity, suggested_solution, now, now))
            await db.commit()
            cap_id = cur.lastrowid or 0
            created = True
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM capability_gaps WHERE id=?", (cap_id,))
        result = _row(await cur.fetchone())
    _publish(
        "capability_gap_added" if created else "capability_gap_updated",
        {"row": result, "created": created, "is_high": severity >= 4},
    )
    return {"ok": True, "created": created, "is_high": severity >= 4, "row": result}


async def list_open() -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM capability_gaps WHERE status='open' ORDER BY severity DESC, last_reported_at DESC")
        return [_row(r) for r in await cur.fetchall()]


async def list_all(limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM capability_gaps ORDER BY last_reported_at DESC LIMIT ?",
            (limit,))
        return [_row(r) for r in await cur.fetchall()]


async def update_status(cap_id: int, status: str, notes: str | None = None) -> bool:
    if status not in {"open", "dismissed", "resolved"}:
        return False
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "UPDATE capability_gaps SET status=?, notes=COALESCE(?, notes) WHERE id=?",
            (status, notes, cap_id))
        await db.commit()
        ok = cur.rowcount > 0
    if ok:
        _publish("capability_gap_updated",
                 {"id": cap_id, "status": status, "notes": notes})
    return ok


async def get(cap_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM capability_gaps WHERE id=?", (cap_id,))
        r = await cur.fetchone()
        return _row(r) if r else None
