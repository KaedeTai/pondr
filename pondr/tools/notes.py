"""Notes — write/list (sqlite-backed)."""
from __future__ import annotations
from ..kb import sqlite as kb_sql


async def note_write(topic: str, content: str) -> dict:
    nid = await kb_sql.add_note(topic, content)
    return {"id": nid, "topic": topic}


async def note_list(filter: str | None = None, limit: int = 50) -> list[dict]:
    return await kb_sql.list_notes(filter, limit=limit)


WRITE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note_write",
        "description": "Save a finding/note keyed by topic.",
        "parameters": {
            "type": "object",
            "properties": {"topic": {"type": "string"}, "content": {"type": "string"}},
            "required": ["topic", "content"],
        },
    },
}
LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note_list",
        "description": "List recent notes, optionally filtered by topic substring.",
        "parameters": {
            "type": "object",
            "properties": {"filter": {"type": "string"}, "limit": {"type": "integer", "default": 50}},
        },
    },
}
