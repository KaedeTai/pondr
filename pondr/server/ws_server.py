"""Backward-compat thin wrapper. Prefer pondr.server.channels.websocket."""
from .channels.websocket import WebSocketChannel

__all__ = ["WebSocketChannel"]
