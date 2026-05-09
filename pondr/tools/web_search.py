"""DuckDuckGo web search tool."""
from __future__ import annotations
import asyncio
from typing import Any
from ..utils.log import logger


async def web_search(q: str, n: int = 8) -> list[dict]:
    def _do():
        try:
            from duckduckgo_search import DDGS
            with DDGS() as d:
                return list(d.text(q, max_results=max(1, min(int(n), 30))))
        except Exception as e:
            logger.warning(f"web_search failed: {e}")
            return [{"error": repr(e), "query": q}]
    return await asyncio.to_thread(_do)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web via DuckDuckGo. Returns a list of {title, href, body}.",
        "parameters": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query"},
                "n": {"type": "integer", "default": 8, "description": "Max results"},
            },
            "required": ["q"],
        },
    },
}
