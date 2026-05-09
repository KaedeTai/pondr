"""Loguru setup + structured event log."""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path
from loguru import logger
from .. import config

logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan>: {message}")
logger.add(config.LOG_DIR / "pondr.log", level="DEBUG", rotation="10 MB", retention=5)

_EVENT_BUF: list[dict] = []
_BUF_MAX = 200


def event(kind: str, **fields) -> dict:
    rec = {"ts": time.time(), "kind": kind, **fields}
    try:
        with config.EVENT_LOG_PATH.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        logger.warning(f"event log write failed: {e}")
    _EVENT_BUF.append(rec)
    if len(_EVENT_BUF) > _BUF_MAX:
        del _EVENT_BUF[: len(_EVENT_BUF) - _BUF_MAX]
    return rec


def recent_events(n: int = 50) -> list[dict]:
    return list(_EVENT_BUF[-n:])


__all__ = ["logger", "event", "recent_events"]
