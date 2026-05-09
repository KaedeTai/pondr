"""Parameterless SQL query tool — read-only across SQLite KB and DuckDB ticks."""
from __future__ import annotations
import asyncio
import aiosqlite
from .. import config
from ..kb import duckdb as ddb
from ..utils.log import logger

ALLOWED = {"kb", "ticks"}


def _is_select(sql: str) -> bool:
    s = sql.strip().lower().lstrip("(")
    return s.startswith("select") or s.startswith("with")


async def sql_query(db: str, sql: str, limit: int = 200) -> dict:
    if db not in ALLOWED:
        return {"error": f"db must be one of {ALLOWED}"}
    if not _is_select(sql):
        return {"error": "only SELECT/WITH queries permitted"}
    try:
        if db == "kb":
            async with aiosqlite.connect(config.DB_KB) as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(sql)
                rows = await cur.fetchmany(limit)
                return {"rows": [dict(r) for r in rows]}
        else:
            return {"rows": (await ddb.query(sql))[:limit]}
    except Exception as e:
        logger.warning(f"sql_query failed: {e}")
        return {"error": repr(e)}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "sql_query",
        "description": "Run a read-only SELECT/WITH query against 'kb' (sqlite) or 'ticks' (duckdb).",
        "parameters": {
            "type": "object",
            "properties": {
                "db": {"type": "string", "enum": ["kb", "ticks"]},
                "sql": {"type": "string"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["db", "sql"],
        },
    },
}
