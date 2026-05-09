"""User-interrupt flag tracked by the research loop.

Channel handlers can call set_interrupt(reason) when an inbound chat message
arrives that the LLM-router classifies as 'interrupt'. The research loop checks
peek_interrupt() between subtasks.
"""
from __future__ import annotations
import asyncio


class _State:
    flag = False
    reason = ""
    cond = asyncio.Lock()


def set_interrupt(reason: str = ""):
    _State.flag = True
    _State.reason = reason


def peek_interrupt() -> tuple[bool, str]:
    return _State.flag, _State.reason


def clear_interrupt():
    _State.flag = False
    _State.reason = ""
