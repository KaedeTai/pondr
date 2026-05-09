"""Async retry with exponential backoff."""
from __future__ import annotations
import asyncio
import random
from typing import Callable, Awaitable, TypeVar
from .log import logger

T = TypeVar("T")


async def with_retry(fn: Callable[[], Awaitable[T]], *,
                     attempts: int = 5,
                     base: float = 1.0,
                     cap: float = 60.0,
                     label: str = "op") -> T:
    last = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as e:
            last = e
            delay = min(cap, base * (2 ** i)) * (0.5 + random.random())
            logger.warning(f"{label} attempt {i+1}/{attempts} failed ({e!r}); sleep {delay:.1f}s")
            await asyncio.sleep(delay)
    raise last  # type: ignore
