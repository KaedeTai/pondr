"""Generic REST call tool."""
from __future__ import annotations
import httpx
from ..utils.log import logger

UA = "pondr/0.1 research-bot"


async def rest_call(url: str, method: str = "GET", headers: dict | None = None,
                    json: dict | None = None, params: dict | None = None,
                    timeout: float = 20.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method.upper(), url,
                                headers={"User-Agent": UA, **(headers or {})},
                                json=json, params=params)
            try:
                body = r.json()
            except Exception:
                body = r.text[:20000]
            return {"status": r.status_code, "url": str(r.url), "body": body}
    except Exception as e:
        logger.warning(f"rest_call {url} failed: {e}")
        return {"error": repr(e), "url": url}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "rest_call",
        "description": "Generic HTTP REST call (GET/POST/PUT/DELETE).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "headers": {"type": "object"},
                "json": {"type": "object"},
                "params": {"type": "object"},
            },
            "required": ["url"],
        },
    },
}
