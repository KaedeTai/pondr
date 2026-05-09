"""LLM-designed strategies + lineage tree.

A strategy is a Python `on_tick(state, tick) -> action` function the LLM wrote
(see pondr.tools.strategy.design_strategy). Each strategy can be iterated into
a child variant — the resulting tree is browsable in the dashboard's Strategy
Lab card and survives across runs.

Schema notes:
  - lineage_parent_id is a self-FK; root strategies have NULL.
  - source_notes is JSON-encoded list of note ids / RAG chunk refs that
    inspired the hypothesis.
  - backtests.strategy_id (added by migration below) lets us link a backtest
    row back to the strategy that produced it.
"""
from __future__ import annotations
import json
import aiosqlite
from .. import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  hypothesis TEXT,
  source_notes TEXT,                    -- JSON list of note ids / chunk refs
  code_python TEXT NOT NULL,
  lineage_parent_id INTEGER REFERENCES strategies(id),
  created_by TEXT DEFAULT 'pondr',      -- 'pondr' | 'user' | 'starter'
  created_at REAL DEFAULT (strftime('%s','now')),
  description TEXT,
  status TEXT DEFAULT 'ok'              -- 'ok' | 'compile_error' | 'runtime_error' | 'retired'
);
CREATE INDEX IF NOT EXISTS idx_strat_parent ON strategies(lineage_parent_id);
CREATE INDEX IF NOT EXISTS idx_strat_created ON strategies(created_at DESC);
"""


async def _table_has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in await cur.fetchall())


async def init():
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        # Migration: link backtests rows to a strategy_id (nullable for back-compat
        # with rows created by the legacy run_backtest path).
        if not await _table_has_column(db, "backtests", "strategy_id"):
            await db.execute(
                "ALTER TABLE backtests ADD COLUMN strategy_id INTEGER "
                "REFERENCES strategies(id)")
        await db.commit()


async def add(name: str, hypothesis: str, code_python: str, *,
              source_notes: list | None = None,
              lineage_parent_id: int | None = None,
              created_by: str = "pondr",
              description: str = "",
              status: str = "ok") -> int:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            """INSERT INTO strategies
               (name, hypothesis, source_notes, code_python,
                lineage_parent_id, created_by, description, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, hypothesis,
             json.dumps(source_notes or []),
             code_python, lineage_parent_id, created_by,
             description, status))
        await db.commit()
        return cur.lastrowid or 0


async def get(strategy_id: int) -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM strategies WHERE id=?", (strategy_id,))
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["source_notes"] = json.loads(d.get("source_notes") or "[]")
        except Exception:
            d["source_notes"] = []
        return d


async def list_all(limit: int = 50,
                   include_retired: bool = False) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        if include_retired:
            cur = await db.execute(
                "SELECT * FROM strategies ORDER BY id DESC LIMIT ?", (limit,))
        else:
            cur = await db.execute(
                "SELECT * FROM strategies WHERE status != 'retired' "
                "ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["source_notes"] = json.loads(d.get("source_notes") or "[]")
            except Exception:
                d["source_notes"] = []
            out.append(d)
        return out


async def descendants(strategy_id: int) -> list[dict]:
    """Recursive children of a strategy (BFS, immediate first)."""
    out: list[dict] = []
    frontier = [strategy_id]
    seen: set[int] = set()
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        while frontier:
            qm = ",".join("?" for _ in frontier)
            cur = await db.execute(
                f"SELECT * FROM strategies WHERE lineage_parent_id IN ({qm})",
                frontier)
            rows = await cur.fetchall()
            frontier = []
            for r in rows:
                d = dict(r)
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                out.append(d)
                frontier.append(d["id"])
    return out


async def lineage_tree(root_id: int) -> dict:
    """Return {root: dict, children: [tree, ...]} — fully nested."""
    root = await get(root_id)
    if root is None:
        return {}
    descs = await descendants(root_id)
    by_parent: dict[int, list[dict]] = {}
    for d in descs:
        by_parent.setdefault(d["lineage_parent_id"], []).append(d)

    def _build(node: dict) -> dict:
        kids = by_parent.get(node["id"], [])
        return {**node, "children": [_build(k) for k in kids]}
    return _build(root)


async def find_root(strategy_id: int) -> int:
    """Walk parents until we find one with NULL parent."""
    cur_id = strategy_id
    async with aiosqlite.connect(config.DB_KB) as db:
        for _ in range(100):  # guard against cycles
            cur = await db.execute(
                "SELECT lineage_parent_id FROM strategies WHERE id=?",
                (cur_id,))
            r = await cur.fetchone()
            if not r or r[0] is None:
                return cur_id
            cur_id = r[0]
    return cur_id


async def last_backtest(strategy_id: int) -> dict | None:
    """Most recent backtest row for this strategy_id, with metrics decoded."""
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, strategy, symbol, n_ticks, metrics_json, ascii_curve, "
            "created_at, confidence FROM backtests WHERE strategy_id=? "
            "ORDER BY id DESC LIMIT 1", (strategy_id,))
        r = await cur.fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
        except Exception:
            d["metrics"] = {}
        return d


async def all_backtests(strategy_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, symbol, n_ticks, metrics_json, ascii_curve, "
            "created_at, confidence FROM backtests WHERE strategy_id=? "
            "ORDER BY id DESC LIMIT ?", (strategy_id, limit))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
            except Exception:
                d["metrics"] = {}
            out.append(d)
        return out


async def update_status(strategy_id: int, status: str) -> bool:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.execute(
            "UPDATE strategies SET status=? WHERE id=?",
            (status, strategy_id))
        await db.commit()
    return True
