"""User preference tools — pref_list / pref_save / pref_delete / pref_search."""
from __future__ import annotations
from ..kb import preferences as prefs_kb


async def pref_list() -> list[dict]:
    return await prefs_kb.list_active()


async def pref_save(key: str, value: str, category: str | None = None,
                    reason: str | None = None) -> dict:
    out = await prefs_kb.save(key, value, category=category,
                              channel="llm-tool", user_msg=reason)
    return out


async def pref_delete(key: str) -> dict:
    return {"ok": await prefs_kb.delete(key, channel="llm-tool")}


async def pref_search(q: str, k: int = 10) -> list[dict]:
    return await prefs_kb.search(q, limit=k)


LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pref_list",
        "description": "List currently-active user preferences.",
        "parameters": {"type": "object", "properties": {}},
    },
}
SAVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pref_save",
        "description": (
            "Persist a user preference (e.g. language, tone, workflow rule). "
            "Use ONLY for long-lived instructions ('always', 'from now on', "
            "'請以後'); never for one-off ad-hoc requests. NEVER store "
            "secrets/keys/passwords/PII."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "short snake_case identifier, e.g. 'language'"},
                "value": {"type": "string"},
                "category": {"type": "string",
                             "enum": ["communication", "workflow", "notification",
                                      "research", "general"]},
                "reason": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
}
DELETE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pref_delete",
        "description": "Remove a user preference by key (when user revokes it).",
        "parameters": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
}
SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "pref_search",
        "description": "Search user preferences by keyword.",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "k": {"type": "integer", "default": 10}},
            "required": ["q"],
        },
    },
}
