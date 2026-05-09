"""ChromaDB wrapper for semantic memory.

Uses default embedding (sentence-transformers if installed, else simple). All
operations wrapped in asyncio.to_thread since chromadb is sync.
"""
from __future__ import annotations
import asyncio
import time
import uuid
from .. import config
from ..utils.log import logger

_client = None
_coll = None


def _get_collection():
    global _client, _coll
    if _coll is not None:
        return _coll
    try:
        import chromadb
        _client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        _coll = _client.get_or_create_collection("pondr_kb")
    except Exception as e:
        logger.warning(f"chroma init failed, RAG disabled: {e}")
        _coll = None
    return _coll


async def init():
    await asyncio.to_thread(_get_collection)


async def store(text: str, meta: dict | None = None) -> str:
    doc_id = str(uuid.uuid4())
    meta = {"ts": time.time(), **(meta or {})}

    def _do():
        c = _get_collection()
        if c is None:
            return doc_id
        try:
            c.add(ids=[doc_id], documents=[text], metadatas=[meta])
        except Exception as e:
            logger.warning(f"chroma store failed: {e}")
        return doc_id

    return await asyncio.to_thread(_do)


async def search(q: str, k: int = 5) -> list[dict]:
    def _do():
        c = _get_collection()
        if c is None:
            return []
        try:
            r = c.query(query_texts=[q], n_results=k)
            ids = (r.get("ids") or [[]])[0]
            docs = (r.get("documents") or [[]])[0]
            metas = (r.get("metadatas") or [[]])[0]
            dist = (r.get("distances") or [[]])[0]
            return [
                {"id": i, "doc": d, "meta": m, "distance": dd}
                for i, d, m, dd in zip(ids, docs, metas, dist)
            ]
        except Exception as e:
            logger.warning(f"chroma search failed: {e}")
            return []

    return await asyncio.to_thread(_do)


async def count() -> int:
    def _do():
        c = _get_collection()
        if c is None:
            return 0
        try:
            return c.count()
        except Exception:
            return 0
    return await asyncio.to_thread(_do)
