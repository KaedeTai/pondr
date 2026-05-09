"""Telegram channel — graceful skip if creds or library missing.

On bot startup, sends a digest of pending questions (only those not yet
delivered to the 'telegram' channel per the sent_to_channels marker).
"""
from __future__ import annotations
import asyncio

from ... import config
from ...kb import questions as q_kb
from ...utils.log import logger
from .base import MessageChannel, list_questions


class TelegramChannel(MessageChannel):
    name = "telegram"

    def __init__(self):
        super().__init__()
        self._app = None
        self._chat_id = config.TELEGRAM_CHAT_ID

    async def start(self):
        if not (config.TELEGRAM_BOT_TOKEN and self._chat_id):
            logger.info("telegram: no token/chat_id, channel disabled")
            return
        try:
            from telegram.ext import Application, MessageHandler, filters
        except Exception as e:
            logger.warning(f"telegram lib missing ({e}), channel disabled")
            return

        async def on_msg(update, _ctx):
            try:
                txt = (update.message.text or "").strip()
                if not txt:
                    return
                pending = await list_questions()
                if pending:
                    last = pending[-1]
                    opts = last.get("options")
                    answer_txt = txt
                    # If question has options and user typed a number, map to that option
                    if opts and txt.isdigit():
                        idx = int(txt) - 1
                        if 0 <= idx < len(opts):
                            answer_txt = str(opts[idx])
                    msg = {"type": "answer", "qid": last["qid"], "text": answer_txt}
                else:
                    msg = {"type": "chat", "text": txt}
                await self._on_inbound(msg)
            except Exception as e:
                logger.warning(f"telegram on_msg: {e}")

        try:
            self._app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
            self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_msg))
            await self._app.initialize()
            await self._app.start()
            asyncio.create_task(self._app.updater.start_polling(),
                                name="telegram-poll")
            self.connected = True
            logger.info("telegram channel ready")
        except Exception as e:
            logger.warning(f"telegram start failed: {e}")
            self._app = None

    async def stop(self):
        if self._app:
            try:
                await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                pass

    async def send(self, msg: dict):
        if not self._app or not self._chat_id:
            return
        try:
            text = self._format(msg)
            await self._app.bot.send_message(chat_id=self._chat_id, text=text)
        except Exception as e:
            logger.warning(f"telegram send: {e}")

    @staticmethod
    def _format(m: dict) -> str:
        t = m.get("type", "msg")
        if t in ("question", "pending_question"):
            opts = m.get("options")
            age = m.get("age_seconds")
            age_s = f" ({int(age/60)}m ago)" if age and age > 60 else ""
            head = f"❓{age_s} {m.get('question','?')}"
            if opts:
                listing = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(opts))
                head += "\nReply with a number or the full text:\n" + listing
            return head
        if t == "question_answered":
            return f"✓ Q answered: {m.get('answer','')[:200]}"
        return f"[{t}] " + (m.get("msg") or m.get("text") or str(m))[:1000]
