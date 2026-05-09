"""Semantic memory tools backed by ChromaDB."""
from __future__ import annotations
from ..kb import chroma


async def rag_search(q: str, k: int = 5) -> list[dict]:
    return await chroma.search(q, k=k)


async def rag_store(text: str, meta: dict | None = None) -> dict:
    doc_id = await chroma.store(text, meta=meta)
    return {"id": doc_id}


SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag_search",
        "description": "Semantic search over the bot's memory (ChromaDB).",
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}, "k": {"type": "integer", "default": 5}},
            "required": ["q"],
        },
    },
}
STORE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "rag_store",
        "description": "Store a piece of text in semantic memory with optional metadata.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "meta": {"type": "object"}},
            "required": ["text"],
        },
    },
}
