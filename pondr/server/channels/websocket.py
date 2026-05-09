"""WebSocket channel — listens on PONDR_WS_PORT and serves multiple clients.

On every new connection, after the hello packet, replay all currently-pending
questions so a freshly-(re)connected client can see anything outstanding.
"""
from __future__ import annotations
import asyncio
import json
import time
import websockets

from ... import config
from ...kb import questions as q_kb
from ...utils.log import logger
from .base import MessageChannel


class WebSocketChannel(MessageChannel):
    name = "ws"

    def __init__(self, host: str | None = None, port: int | None = None):
        super().__init__()
        self.host = host or config.BIND_HOST
        self.port = port or config.WS_PORT
        self._clients: set = set()
        self._server = None

    async def start(self):
        async def _handler(ws):
            self._clients.add(ws)
            await ws.send(json.dumps({"type": "hello", "ts": time.time()}))
            # Replay all pending questions to THIS client
            try:
                pending = await q_kb.list_pending()
                for q in pending:
                    await ws.send(json.dumps({
                        "type": "pending_question",
                        "qid": q["qid"],
                        "question": q["question"],
                        "options": q.get("options"),
                        "asked_at": q.get("asked_at"),
                        "asked_by": q.get("asked_by"),
                        "age_seconds": q.get("age_seconds"),
                    }, default=str, ensure_ascii=False))
            except Exception as e:
                logger.warning(f"ws replay-on-connect: {e}")
            try:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        msg = {"type": "chat", "text": str(raw)}
                    await self._on_inbound(msg)
            except Exception as e:
                logger.debug(f"ws client closed: {e}")
            finally:
                self._clients.discard(ws)

        self._server = await websockets.serve(_handler, self.host, self.port)
        self.connected = True
        logger.info(f"ws channel listening on ws://{self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.connected = False

    async def send(self, msg: dict):
        if not self._clients:
            return
        data = json.dumps(msg, default=str, ensure_ascii=False)
        dead = []
        for c in list(self._clients):
            try:
                await c.send(data)
            except Exception:
                dead.append(c)
        for d in dead:
            self._clients.discard(d)
