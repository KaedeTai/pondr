"""Knowledge gap registry — what bot knows vs doesn't know per topic."""
from __future__ import annotations
import json
import time
import aiosqlite
from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    sub_question TEXT NOT NULL,
    status TEXT DEFAULT 'unknown',     -- unknown / researching / known
    answer_summary TEXT,
    sources TEXT,                       -- JSON array
    created_at REAL,
    updated_at REAL,
    UNIQUE(topic, sub_question)
);
CREATE INDEX IF NOT EXISTS idx_kg_topic ON knowledge_gaps(topic);
CREATE INDEX IF NOT EXISTS idx_kg_status ON knowledge_gaps(status);
"""


def _row(r: aiosqlite.Row) -> dict:
    d = dict(r)
    try:
        d["sources"] = json.loads(d.get("sources") or "[]")
    except Exception:
        d["sources"] = []
    return d


async def init() -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert(topic: str, sub_question: str,
                 status: str = "unknown",
                 answer_summary: str | None = None,
                 sources: list[str] | None = None) -> int:
    """Insert or update a (topic, sub_question) pair. Returns the row id."""
    now = time.time()
    src_json = json.dumps(sources or [])
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT id FROM knowledge_gaps WHERE topic=? AND sub_question=?",
            (topic, sub_question))
        row = await cur.fetchone()
        if row:
            await db.execute(
                """UPDATE knowledge_gaps
                   SET status=?, answer_summary=COALESCE(?, answer_summary),
                       sources=?, updated_at=?
                   WHERE id=?""",
                (status, answer_summary, src_json, now, row[0]))
            await db.commit()
            return int(row[0])
        cur = await db.execute(
            """INSERT INTO knowledge_gaps
               (topic, sub_question, status, answer_summary, sources,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (topic, sub_question, status, answer_summary, src_json, now, now))
        await db.commit()
        return cur.lastrowid or 0


async def list_by_topic(topic: str | None = None,
                        status: str | None = None,
                        limit: int = 200) -> list[dict]:
    where = []
    params: list = []
    if topic:
        where.append("topic LIKE ?")
        params.append(f"%{topic}%")
    if status:
        where.append("status=?")
        params.append(status)
    sql = "SELECT * FROM knowledge_gaps"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY topic, status, updated_at DESC LIMIT ?"
    params.append(limit)
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, params)
        return [_row(r) for r in await cur.fetchall()]


async def tree() -> dict[str, list[dict]]:
    """Return all rows grouped by topic."""
    rows = await list_by_topic()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["topic"], []).append(r)
    return out


async def counts_by_status() -> dict[str, int]:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM knowledge_gaps GROUP BY status")
        return {r[0]: r[1] for r in await cur.fetchall()}


async def mark_status(gap_id: int, status: str,
                      answer_summary: str | None = None,
                      sources: list[str] | None = None) -> bool:
    if status not in {"unknown", "researching", "known"}:
        return False
    async with aiosqlite.connect(config.DB_KB) as db:
        params: list = [status]
        sets = ["status=?"]
        if answer_summary is not None:
            sets.append("answer_summary=?")
            params.append(answer_summary)
        if sources is not None:
            sets.append("sources=?")
            params.append(json.dumps(sources))
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(gap_id)
        cur = await db.execute(
            f"UPDATE knowledge_gaps SET {', '.join(sets)} WHERE id=?", params)
        await db.commit()
        return cur.rowcount > 0
