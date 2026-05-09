"""MessageChannel ABC + ChannelMux + persistent ask_user.

Pending questions are persisted to SQLite (`pondr.kb.questions`). They survive
bot restarts. Channels replay pending on (re)connect. Answers update the row
AND resolve any in-process future awaiting the qid.
"""
from __future__ import annotations
import abc
import asyncio
import time
import uuid
from typing import AsyncIterator

from ...kb import questions as q_kb
from ...utils.log import logger, event


class MessageChannel(abc.ABC):
    """Bidirectional message channel."""
    name: str = "channel"

    def __init__(self):
        self._inbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self.connected = False

    @abc.abstractmethod
    async def start(self) -> None: ...

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, msg: dict) -> None: ...

    async def receive(self) -> AsyncIterator[dict]:
        while True:
            yield await self._inbox.get()

    async def _on_inbound(self, msg: dict):
        msg.setdefault("channel", self.name)
        await self._inbox.put(msg)

    async def replay_pending(self, mark_sent: bool = False) -> int:
        """Push all pending questions to this channel.

        mark_sent=True records that this channel has delivered them (used by
        once-per-restart channels like telegram/stdio). WS sends to each new
        client without marking, since clients come and go independently.
        """
        rows = await q_kb.list_pending()
        n = 0
        for q in rows:
            if mark_sent and q.get("sent_to_channels") and self.name in q["sent_to_channels"]:
                continue
            try:
                await self.send({
                    "type": "pending_question",
                    "qid": q["qid"],
                    "question": q["question"],
                    "options": q.get("options"),
                    "asked_at": q.get("asked_at"),
                    "asked_by": q.get("asked_by"),
                    "age_seconds": q.get("age_seconds"),
                })
                n += 1
                if mark_sent:
                    await q_kb.mark_sent(q["qid"], self.name)
            except Exception as e:
                logger.warning(f"replay {self.name} qid={q['qid']}: {e}")
        return n


# ---- In-process futures awaiting answers ----
_PENDING: dict[str, asyncio.Future] = {}


async def resolve_question(qid: str, answer: str, via: str = "") -> bool:
    """Persist the answer + resolve any awaiting future + broadcast notice.

    Returns True if this call moved a row from pending → answered. Subsequent
    calls for the same qid are no-ops (return False)."""
    moved = await q_kb.mark_answered(qid, answer, via or "unknown")
    if moved:
        event("ask_user_answered", qid=qid, answer=answer, via=via)
        # Resolve in-process future (if any)
        fut = _PENDING.pop(qid, None)
        if fut and not fut.done():
            fut.set_result(answer)
        # Broadcast to all channels so other clients stop showing the prompt
        try:
            await MUX.send({"type": "question_answered",
                            "qid": qid, "answer": answer, "via": via})
        except Exception:
            pass
    return moved


async def list_questions() -> list[dict]:
    """Snapshot of currently-pending questions (DB query)."""
    return await q_kb.list_pending()


async def _resolve_option_index(qid: str, raw: str) -> str:
    """If qid has options[] and raw is just '1', '2', ... map to that option."""
    text = (raw or "").strip()
    if not text or not text.isdigit():
        return raw
    try:
        rows = await q_kb.list_pending()
        for q in rows:
            if q.get("qid") == qid:
                opts = q.get("options") or []
                idx = int(text) - 1
                if 0 <= idx < len(opts):
                    return str(opts[idx])
                break
    except Exception:
        pass
    return raw


class ChannelMux:
    """Manages multiple channels — broadcast send, race ask, merged receive."""

    def __init__(self):
        self.channels: list[MessageChannel] = []
        self._merged: asyncio.Queue[dict] = asyncio.Queue(maxsize=400)
        self._readers: list[asyncio.Task] = []
        self._sweep_task: asyncio.Task | None = None

    def add(self, ch: MessageChannel):
        self.channels.append(ch)

    async def start_all(self):
        await q_kb.init()
        for ch in self.channels:
            try:
                await ch.start()
                logger.info(f"channel started: {ch.name}")
            except Exception as e:
                logger.warning(f"channel {ch.name} failed to start: {e}")
            self._readers.append(asyncio.create_task(self._reader(ch),
                                                    name=f"chmux-rd-{ch.name}"))
        # Replay pending to single-recipient channels (stdio/telegram)
        for ch in self.channels:
            if ch.name in ("stdio", "telegram"):
                try:
                    n = await ch.replay_pending(mark_sent=True)
                    if n:
                        logger.info(f"replayed {n} pending to {ch.name}")
                except Exception as e:
                    logger.warning(f"replay-on-start {ch.name}: {e}")
        # Start timeout sweep
        self._sweep_task = asyncio.create_task(self._sweep(), name="qtimeout-sweep")

    async def stop_all(self):
        for t in self._readers:
            t.cancel()
        if self._sweep_task:
            self._sweep_task.cancel()
        for ch in self.channels:
            try:
                await ch.stop()
            except Exception:
                pass

    async def _reader(self, ch: MessageChannel):
        async for msg in ch.receive():
            try:
                # Inline answer handling so future resolves immediately
                if msg.get("type") == "answer" and msg.get("qid"):
                    raw = msg.get("text") or msg.get("answer") or ""
                    answer = await _resolve_option_index(msg["qid"], raw)
                    await resolve_question(msg["qid"], answer, via=ch.name)
                    continue
                await self._merged.put(msg)
            except Exception as e:
                logger.warning(f"reader {ch.name}: {e}")

    async def send(self, msg: dict):
        for ch in self.channels:
            try:
                await ch.send(msg)
            except Exception as e:
                logger.warning(f"send via {ch.name} failed: {e}")

    async def messages(self) -> AsyncIterator[dict]:
        while True:
            yield await self._merged.get()

    async def ask(self, question: str, options: list[str] | None = None,
                  timeout: float | None = None,
                  asked_by: str | None = None) -> str:
        qid = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _PENDING[qid] = fut
        timeout_at = (time.time() + timeout) if timeout else None
        await q_kb.add(qid, question, options, asked_by, timeout_at)
        event("ask_user", qid=qid, question=question, options=options,
              asked_by=asked_by, timeout=timeout)
        await self.send({"type": "question", "qid": qid,
                         "question": question, "options": options,
                         "asked_by": asked_by, "asked_at": time.time()})
        try:
            if timeout:
                ans = await asyncio.wait_for(fut, timeout=timeout)
            else:
                ans = await fut
            return ans
        except asyncio.TimeoutError:
            await q_kb.mark_timeout(qid)
            event("ask_user_timeout", qid=qid)
            try:
                await self.send({"type": "question_timed_out", "qid": qid})
            except Exception:
                pass
            raise
        finally:
            _PENDING.pop(qid, None)

    async def _sweep(self):
        """Mark expired pending questions and resolve their futures."""
        while True:
            try:
                await asyncio.sleep(10)
                expired = await q_kb.list_expired()
                for q in expired:
                    qid = q["qid"]
                    if await q_kb.mark_timeout(qid):
                        event("ask_user_timeout_swept", qid=qid)
                        fut = _PENDING.pop(qid, None)
                        if fut and not fut.done():
                            fut.set_exception(asyncio.TimeoutError())
                        try:
                            await self.send({"type": "question_timed_out", "qid": qid})
                        except Exception:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"sweep: {e}")


# Module-level singleton — populated by orchestrator on boot.
MUX: ChannelMux = ChannelMux()


async def ask_user(question: str, options: list[str] | None = None,
                   timeout_s: float | None = None,
                   asked_by: str | None = None) -> str:
    """Public entry — bot asks human via all active channels."""
    return await MUX.ask(question, options=options, timeout=timeout_s,
                         asked_by=asked_by)
