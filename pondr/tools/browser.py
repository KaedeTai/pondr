"""Playwright browser fetch (skeleton). TODO: full impl + chromium install.

If playwright/chromium is available we use it; otherwise we fall back to
web_fetch and return a flag indicating that JS rendering was skipped.
"""
from __future__ import annotations
from .web_fetch import web_fetch
from ..utils.log import logger


async def browser_fetch(url: str, wait_ms: int = 2000) -> dict:
    try:
        from playwright.async_api import async_playwright
    except Exception:
        logger.info("playwright not installed, falling back to web_fetch")
        out = await web_fetch(url)
        out["js_rendered"] = False
        return out
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(wait_ms)
            html = await page.content()
            title = await page.title()
            await browser.close()
            return {"url": url, "title": title, "html": html[:50000], "js_rendered": True}
    except Exception as e:
        logger.warning(f"browser_fetch failed, fallback to web_fetch: {e}")
        out = await web_fetch(url)
        out["js_rendered"] = False
        out["browser_error"] = repr(e)
        return out


SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser_fetch",
        "description": "JS-render a page via Playwright (falls back to plain fetch).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait_ms": {"type": "integer", "default": 2000},
            },
            "required": ["url"],
        },
    },
}
