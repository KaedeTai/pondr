"""SQLite KB — tasks, sources, decisions, notes."""
from __future__ import annotations
import aiosqlite
from .. import config


def _publish(event_type: str, payload: dict) -> None:
    """Best-effort fanout to the dashboard SSE event_bus."""
    try:
        from ..server import event_bus
        event_bus.publish(event_type, payload)
    except Exception:
        pass

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT NOT NULL,
  description TEXT,
  status TEXT DEFAULT 'queued',
  parent_id INTEGER,
  created_at REAL DEFAULT (strftime('%s', 'now')),
  updated_at REAL DEFAULT (strftime('%s', 'now')),
  result TEXT
);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT,
  title TEXT,
  body TEXT,
  fetched_at REAL DEFAULT (strftime('%s', 'now')),
  task_id INTEGER
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  decision TEXT,
  rationale TEXT,
  created_at REAL DEFAULT (strftime('%s', 'now'))
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  topic TEXT,
  content TEXT,
  created_at REAL DEFAULT (strftime('%s', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_notes_topic ON notes(topic);
CREATE TABLE IF NOT EXISTS backtests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  strategy TEXT NOT NULL,
  symbol TEXT NOT NULL,
  start_ts REAL,
  end_ts REAL,
  n_ticks INTEGER,
  metrics_json TEXT,
  equity_blob TEXT,           -- compressed JSON {ts:[], eq:[]}
  ascii_curve TEXT,
  report_md TEXT,
  created_at REAL DEFAULT (strftime('%s','now')),
  confidence REAL
);
CREATE INDEX IF NOT EXISTS idx_bt_strat ON backtests(strategy, symbol);
CREATE TABLE IF NOT EXISTS arb_opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  ts REAL NOT NULL,
  buy_exchange TEXT,
  sell_exchange TEXT,
  buy_price REAL,
  sell_price REAL,
  spread_bp REAL,
  net_spread_bp REAL,
  fee_bp REAL,
  notional_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_arb_ts ON arb_opportunities(ts DESC);
CREATE INDEX IF NOT EXISTS idx_arb_sym ON arb_opportunities(symbol, ts DESC);
"""


async def _table_has_column(db, table: str, column: str) -> bool:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return any(r[1] == column for r in await cur.fetchall())


async def init():
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        # Migrations — additive columns on notes
        for col, ddl in [("confidence", "REAL"),
                         ("confidence_reason", "TEXT"),
                         ("source_count", "INTEGER DEFAULT 1")]:
            if not await _table_has_column(db, "notes", col):
                await db.execute(f"ALTER TABLE notes ADD COLUMN {col} {ddl}")
        await db.commit()


async def add_task(topic: str, description: str = "", parent_id: int | None = None) -> int:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "INSERT INTO tasks(topic, description, parent_id) VALUES (?,?,?)",
            (topic, description, parent_id),
        )
        await db.commit()
        tid = cur.lastrowid or 0
    _publish("task_added", {"id": tid, "topic": topic,
                            "description": description,
                            "parent_id": parent_id, "status": "queued"})
    return tid


async def next_task() -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM tasks WHERE status='queued' ORDER BY id ASC LIMIT 1")
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute(
            "UPDATE tasks SET status='running', updated_at=strftime('%s','now') WHERE id=?",
            (row["id"],),
        )
        await db.commit()
        return dict(row)


async def complete_task(task_id: int, result: str = ""):
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.execute(
            "UPDATE tasks SET status='done', result=?, updated_at=strftime('%s','now') WHERE id=?",
            (result, task_id),
        )
        await db.commit()
    _publish("task_update", {"id": task_id, "status": "done"})


async def fail_task(task_id: int, err: str):
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.execute(
            "UPDATE tasks SET status='failed', result=?, updated_at=strftime('%s','now') WHERE id=?",
            (err, task_id),
        )
        await db.commit()
    _publish("task_update", {"id": task_id, "status": "failed",
                              "error": err[:300]})


async def list_tasks(status: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit))
        else:
            cur = await db.execute(
                "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def add_source(url: str, title: str, body: str, task_id: int | None = None) -> int:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "INSERT INTO sources(url,title,body,task_id) VALUES(?,?,?,?)",
            (url, title, body, task_id))
        await db.commit()
        return cur.lastrowid or 0


async def add_note(topic: str, content: str,
                   confidence: float | None = None,
                   confidence_reason: str | None = None,
                   source_count: int = 1) -> int:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            """INSERT INTO notes(topic, content, confidence, confidence_reason, source_count)
               VALUES(?, ?, ?, ?, ?)""",
            (topic, content, confidence, confidence_reason, source_count))
        await db.commit()
        nid = cur.lastrowid or 0
    if (topic or "").startswith("finding/"):
        _publish("finding_added", {
            "id": nid,
            "topic": topic,
            "content": (content or "")[:400],
            "confidence": confidence,
            "source_count": source_count,
        })
    return nid


async def update_note_confidence(note_id: int, confidence: float,
                                  reason: str | None = None,
                                  source_count: int | None = None) -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        if source_count is None:
            await db.execute(
                "UPDATE notes SET confidence=?, confidence_reason=COALESCE(?, confidence_reason) WHERE id=?",
                (confidence, reason, note_id))
        else:
            await db.execute(
                "UPDATE notes SET confidence=?, confidence_reason=COALESCE(?, confidence_reason), source_count=? WHERE id=?",
                (confidence, reason, source_count, note_id))
        await db.commit()


async def find_notes_by_topic(topic_prefix: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM notes WHERE topic LIKE ? ORDER BY id DESC LIMIT ?",
            (f"{topic_prefix}%", limit))
        return [dict(r) for r in await cur.fetchall()]


async def list_notes(topic_like: str | None = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        if topic_like:
            cur = await db.execute(
                "SELECT * FROM notes WHERE topic LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{topic_like}%", limit))
        else:
            cur = await db.execute("SELECT * FROM notes ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def counts() -> dict:
    out = {}
    async with aiosqlite.connect(config.DB_KB) as db:
        for t in ("tasks", "sources", "notes", "decisions"):
            cur = await db.execute(f"SELECT COUNT(*) FROM {t}")
            r = await cur.fetchone()
            out[t] = r[0] if r else 0
    return out
