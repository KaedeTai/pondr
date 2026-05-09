"""Channel registry + builder."""
from __future__ import annotations
from ... import config
from ...utils.log import logger
from .base import (MessageChannel, ChannelMux, MUX, ask_user,
                   list_questions, resolve_question)
from .websocket import WebSocketChannel
from .stdio import StdioChannel
from .telegram import TelegramChannel


def build_channels(enabled: list[str] | None = None) -> ChannelMux:
    enabled = enabled or config.CHANNELS_ENABLED
    MUX.channels.clear()
    if "ws" in enabled:
        MUX.add(WebSocketChannel())
    if "stdio" in enabled:
        MUX.add(StdioChannel())
    if "telegram" in enabled:
        MUX.add(TelegramChannel())
    if not MUX.channels:
        logger.warning("no channels enabled")
    return MUX


__all__ = ["MessageChannel", "ChannelMux", "MUX", "ask_user",
           "list_questions", "resolve_question", "build_channels",
           "WebSocketChannel", "StdioChannel", "TelegramChannel"]
