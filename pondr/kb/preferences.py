"""User preferences — SQLite + human-editable preferences.md sync.

Auto-detected from chat by the LLM intent router; injected into every
LLM call's system prompt so the bot honors them across all tasks.
"""
from __future__ import annotations
import asyncio
import re
import time
from typing import Any
import aiosqlite
from .. import config
from ..utils.log import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT,
    added_at REAL NOT NULL,
    added_via_channel TEXT,
    added_via_user_msg TEXT,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS user_preferences_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT,
    category TEXT,
    op TEXT,                       -- 'set' / 'delete' / 'replace'
    prev_value TEXT,
    changed_at REAL NOT NULL,
    via_channel TEXT,
    via_user_msg TEXT
);
CREATE INDEX IF NOT EXISTS idx_pref_active ON user_preferences(active);
CREATE INDEX IF NOT EXISTS idx_pref_hist_key ON user_preferences_history(key);
"""

# Block these from ever being persisted as preferences
SENSITIVE_RE = re.compile(
    r"(api[_\- ]?key|secret|password|passwd|token|credential|ssn|bank|"
    r"credit\s*card|私鑰|密碼|密鑰|帳號)",
    re.IGNORECASE)


def _row_to_dict(row: aiosqlite.Row) -> dict:
    return dict(row)


async def init() -> None:
    async with aiosqlite.connect(config.DB_KB) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    # Re-sync the human-editable file from SQLite (always source of truth)
    try:
        await _sync_md()
    except Exception as e:
        logger.warning(f"prefs.md sync on init: {e}")


def _is_sensitive(*parts: str) -> bool:
    return any(SENSITIVE_RE.search(s or "") for s in parts)


async def save(key: str, value: str, category: str | None = None,
               channel: str | None = None,
               user_msg: str | None = None) -> dict:
    """Insert or replace a preference (audited in history)."""
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return {"ok": False, "error": "key and value required"}
    if _is_sensitive(key, value):
        return {"ok": False, "error": "sensitive content blocked"}
    now = time.time()
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT value, category FROM user_preferences WHERE key=?", (key,))
        prev = await cur.fetchone()
        op = "replace" if prev else "set"
        if prev:
            await db.execute(
                """UPDATE user_preferences SET value=?, category=COALESCE(?, category),
                       added_at=?, added_via_channel=?, added_via_user_msg=?, active=1
                       WHERE key=?""",
                (value, category, now, channel, user_msg, key))
        else:
            await db.execute(
                """INSERT INTO user_preferences
                   (key, value, category, added_at, added_via_channel,
                    added_via_user_msg, active)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (key, value, category, now, channel, user_msg))
        await db.execute(
            """INSERT INTO user_preferences_history
               (key, value, category, op, prev_value, changed_at, via_channel, via_user_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, value, category, op, prev[0] if prev else None,
             now, channel, user_msg))
        await db.commit()
    await _sync_md()
    try:
        from ..server import event_bus
        event_bus.publish("preference_changed",
                          {"key": key, "value": value,
                           "category": category, "op": op})
    except Exception:
        pass
    return {"ok": True, "key": key, "value": value, "op": op}


async def delete(key: str, channel: str | None = None,
                 user_msg: str | None = None) -> bool:
    async with aiosqlite.connect(config.DB_KB) as db:
        cur = await db.execute(
            "SELECT value, category FROM user_preferences WHERE key=? AND active=1", (key,))
        row = await cur.fetchone()
        if not row:
            return False
        await db.execute("UPDATE user_preferences SET active=0 WHERE key=?", (key,))
        await db.execute(
            """INSERT INTO user_preferences_history
               (key, value, category, op, prev_value, changed_at, via_channel, via_user_msg)
               VALUES (?, ?, ?, 'delete', ?, ?, ?, ?)""",
            (key, None, row[1], row[0], time.time(), channel, user_msg))
        await db.commit()
    await _sync_md()
    try:
        from ..server import event_bus
        event_bus.publish("preference_changed", {"key": key, "op": "delete"})
    except Exception:
        pass
    return True


async def list_active() -> list[dict]:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_preferences WHERE active=1 ORDER BY category, key")
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def search(q: str, limit: int = 10) -> list[dict]:
    """Simple LIKE-based search across key/value/category."""
    pat = f"%{q}%"
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM user_preferences
               WHERE active=1 AND (key LIKE ? OR value LIKE ? OR category LIKE ?)
               ORDER BY key LIMIT ?""",
            (pat, pat, pat, limit))
        return [_row_to_dict(r) for r in await cur.fetchall()]


async def get(key: str) -> dict | None:
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM user_preferences WHERE key=? AND active=1", (key,))
        r = await cur.fetchone()
        return _row_to_dict(r) if r else None


def get_language(rows: list[dict]) -> str | None:
    """Return the active language preference value from a pre-fetched row list.

    Kept for legacy call sites (e.g. ask.py, scripts/) that already loaded
    `list_active()` for their own reasons.
    """
    for r in rows:
        if r.get("key", "").lower() == "language":
            return (r.get("value") or "").strip() or None
    return None


async def get_active_language() -> str | None:
    """Convenience: fetch the active language preference value, or None.

    User-facing LLM call sites use this to populate `llm.chat(language_hint=…)`
    so only their output gets the language directive — internal/routing/
    planning calls stay overhead-free.
    """
    pref = await get("language")
    return (pref.get("value") or "").strip() or None if pref else None


async def _sync_md() -> None:
    """Write a fresh preferences.md mirroring SQLite state (best effort)."""
    rows = await list_active()
    by_cat: dict[str, list[str]] = {}
    for r in rows:
        cat = r.get("category") or "general"
        ts = time.strftime("%Y-%m-%d", time.localtime(r.get("added_at") or time.time()))
        via = r.get("added_via_channel") or "?"
        by_cat.setdefault(cat, []).append(
            f"- {r['key']}: {r['value']} (added {ts} via {via})")
    out = ["# User preferences", ""]
    if not rows:
        out.append("(empty — bot will populate based on user instructions)")
    else:
        for cat in sorted(by_cat):
            out.append(f"## {cat.capitalize()}")
            out.extend(by_cat[cat])
            out.append("")
    try:
        config.PREFS_MD_PATH.write_text("\n".join(out))
    except Exception as e:
        logger.warning(f"prefs.md write: {e}")
