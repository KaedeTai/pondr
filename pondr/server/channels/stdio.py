"""Stdio channel — output-only on non-tty stdin (nohup), interactive on tty.

Format on output: tagged JSON (`>>> {json}`). On input: raw text becomes a
chat message OR an answer to the most recent pending question. JSON input is
passed through verbatim.
"""
from __future__ import annotations
import asyncio
import json
import sys
from typing import Any

from ...utils.log import logger
from .base import MessageChannel, list_questions


class StdioChannel(MessageChannel):
    name = "stdio"

    def __init__(self):
        super().__init__()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self):
        if not sys.stdin or not sys.stdin.readable() or not sys.stdin.isatty():
            logger.info("stdio channel: stdin not a tty, output-only mode")
            self.connected = True
            return
        self._task = asyncio.create_task(self._read_loop(), name="stdio-rd")
        self.connected = True
        logger.info("stdio channel ready (type messages on stdin)")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
        self.connected = False

    async def send(self, msg: dict):
        try:
            print(f">>> {json.dumps(msg, default=str, ensure_ascii=False)}", flush=True)
        except Exception:
            pass

    async def _read_loop(self):
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        try:
            await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        except Exception as e:
            logger.warning(f"stdio: cannot connect_read_pipe: {e}")
            return
        while not self._stop.is_set():
            try:
                line = await reader.readline()
            except Exception:
                break
            if not line:
                await asyncio.sleep(0.5)
                continue
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            msg: dict[str, Any]
            if text.startswith("{"):
                try:
                    msg = json.loads(text)
                except Exception:
                    msg = {"type": "chat", "text": text}
            else:
                pending = await list_questions()
                if pending:
                    last = pending[-1]
                    opts = last.get("options")
                    answer_txt = text
                    if opts and text.isdigit():
                        idx = int(text) - 1
                        if 0 <= idx < len(opts):
                            answer_txt = str(opts[idx])
                    msg = {"type": "answer", "qid": last["qid"], "text": answer_txt}
                else:
                    msg = {"type": "chat", "text": text}
            await self._on_inbound(msg)
