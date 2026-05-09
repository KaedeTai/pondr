"""HTTP fetch + readable text extraction via BeautifulSoup."""
from __future__ import annotations
import httpx
from bs4 import BeautifulSoup
from ..utils.log import logger

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X) pondr/0.1 research-bot"


async def web_fetch(url: str, timeout: float = 20.0) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": UA},
                                     follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            if "html" not in ct and "xml" not in ct and "text" not in ct:
                return {"url": str(r.url), "status": r.status_code,
                        "content_type": ct, "text": "", "binary_size": len(r.content)}
            soup = BeautifulSoup(r.text, "lxml")
            for s in soup(["script", "style", "noscript"]):
                s.decompose()
            title = (soup.title.string.strip() if soup.title and soup.title.string else "")
            txt = " ".join(soup.get_text(" ").split())
            return {"url": str(r.url), "status": r.status_code,
                    "content_type": ct, "title": title, "text": txt[:20000]}
    except Exception as e:
        logger.warning(f"web_fetch {url} failed: {e}")
        return {"url": url, "error": repr(e)}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Fetch a URL via httpx and return readable text (BeautifulSoup).",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}
