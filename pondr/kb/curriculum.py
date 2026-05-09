"""Curriculum — textbook-style tree of what pondr has learned.

A self-organizing chapter / section / leaf hierarchy that aggregates notes,
backtests, and knowledge gaps into a learner-friendly view. Generated
periodically by ``pondr.research.curriculum`` and read by the dashboard
"📚 What I've learned" card.

Status semantics:
    "solid"  — confident & well-sourced (avg conf ≥ 0.75 and ≥ 3 notes)
    "medium" — partial (some notes/conf 0.5-0.75, OR ≥ 1 supporting backtest)
    "open"   — researching / unknown (few notes, low conf, or gap-only)
"""
from __future__ import annotations
import json
import time
from typing import Iterable

import aiosqlite

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS curriculum (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT DEFAULT 'open',
    note_ids TEXT,                   -- JSON array of ints
    related_backtest_ids TEXT,       -- JSON array
    related_gap_ids TEXT,            -- JSON array
    avg_confidence REAL,
    mastery_pct REAL,
    last_updated REAL DEFAULT (strftime('%s','now')),
    sort_order INTEGER DEFAULT 0,
    FOREIGN KEY (parent_id) REFERENCES curriculum(id)
);
CREATE INDEX IF NOT EXISTS idx_curr_parent ON curriculum(parent_id);
CREATE INDEX IF NOT EXISTS idx_curr_status ON curriculum(status);
"""


async def init() -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        await db.commit()


def _json_or_none(s: str | None):
    if not s:
        return []
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


async def list_all() -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM curriculum ORDER BY parent_id, sort_order, id")
        rows = []
        for r in await cur.fetchall():
            d = dict(r)
            d["note_ids"] = _json_or_none(d.get("note_ids"))
            d["related_backtest_ids"] = _json_or_none(d.get("related_backtest_ids"))
            d["related_gap_ids"] = _json_or_none(d.get("related_gap_ids"))
            rows.append(d)
        return rows


async def tree() -> list[dict]:
    """Return rows organised as a nested tree of dicts with .children."""
    rows = await list_all()
    by_id = {r["id"]: {**r, "children": []} for r in rows}
    roots: list[dict] = []
    for r in rows:
        node = by_id[r["id"]]
        pid = r.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


async def get_node(node_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM curriculum WHERE id=?", (node_id,))
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        d["note_ids"] = _json_or_none(d.get("note_ids"))
        d["related_backtest_ids"] = _json_or_none(d.get("related_backtest_ids"))
        d["related_gap_ids"] = _json_or_none(d.get("related_gap_ids"))
        return d


async def overall_mastery() -> float:
    """Weighted average of root-level mastery percentages."""
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT mastery_pct FROM curriculum WHERE parent_id IS NULL")
        rows = [r[0] or 0.0 for r in await cur.fetchall()]
    if not rows:
        return 0.0
    return sum(rows) / len(rows)


async def counts_by_status() -> dict[str, int]:
    async with aiosqlite.connect(config.DB_KB) as db:
        out: dict[str, int] = {"solid": 0, "medium": 0, "open": 0}
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM curriculum GROUP BY status")
        for status, n in await cur.fetchall():
            out[status or "open"] = n
        return out


async def replace_tree(spec: list[dict]) -> dict:
    """Atomically replace the entire curriculum with a new tree spec.

    spec is a list of root dicts; each dict may have a ``children`` list.
    Per-node fields: title (req), description, status, note_ids,
    related_backtest_ids, related_gap_ids, avg_confidence, mastery_pct.
    Returns counts.
    """
    inserted = 0
    now = time.time()

    async with aiosqlite.connect(config.DB_KB) as db:
        await db.execute("BEGIN")
        try:
            # Wipe existing tree
            await db.execute("DELETE FROM curriculum")
            order_counter = [0]

            async def _insert(node: dict, parent_id: int | None) -> int:
                nonlocal inserted
                title = (node.get("title") or "").strip()
                if not title:
                    return 0
                order_counter[0] += 1
                cur = await db.execute(
                    """INSERT INTO curriculum
                       (parent_id, title, description, status,
                        note_ids, related_backtest_ids, related_gap_ids,
                        avg_confidence, mastery_pct, last_updated, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (parent_id,
                     title,
                     (node.get("description") or "")[:1000],
                     (node.get("status") or "open"),
                     json.dumps(list(node.get("note_ids") or [])),
                     json.dumps(list(node.get("related_backtest_ids") or [])),
                     json.dumps(list(node.get("related_gap_ids") or [])),
                     float(node.get("avg_confidence") or 0.0),
                     float(node.get("mastery_pct") or 0.0),
                     now,
                     order_counter[0]))
                inserted += 1
                node_id = cur.lastrowid or 0
                for child in (node.get("children") or []):
                    await _insert(child, node_id)
                return node_id

            for root in spec:
                await _insert(root, None)
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise
    try:
        from ..server import event_bus
        event_bus.publish("curriculum_updated", {"inserted": inserted})
    except Exception:
        pass
    return {"inserted": inserted}


async def deep_dive_task_title(node_id: int) -> str | None:
    """Build a research-task title for 'deep dive on this node'."""
    n = await get_node(node_id)
    if not n:
        return None
    return f"[deep-dive] {n['title']}"


def aggregate_mastery_from_children(children: Iterable[dict]) -> float:
    """Average mastery_pct across a list of child dicts (already 0-100)."""
    vals = [float(c.get("mastery_pct") or 0.0) for c in children]
    return sum(vals) / len(vals) if vals else 0.0
