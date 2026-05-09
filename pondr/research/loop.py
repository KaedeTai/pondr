"""Top-level research loop. Never stops."""
from __future__ import annotations
import asyncio
from .. import config
from ..kb import sqlite as kb_sql
from ..server import interrupt as intr
from ..server.channels import MUX
from ..utils.log import logger, event
from .planner import plan
from .executor import execute
from .synthesizer import synthesize
from .reflector import reflect
from .triangulation_ask import triangulate as _triangulate_finding


CURRENT: dict = {"task_id": None, "topic": None, "started": None}


async def _seed_initial():
    """Decompose initial topic into a few sub-areas if KB is empty."""
    rows = await kb_sql.list_tasks()
    if rows:
        return
    seeds = [
        f"{config.INITIAL_TOPIC} — 經典策略 (mean-reversion, momentum, stat-arb)",
        f"{config.INITIAL_TOPIC} — 風險管理與部位管控",
        f"{config.INITIAL_TOPIC} — 市場微結構 (order book, market impact)",
        f"{config.INITIAL_TOPIC} — Crypto 特殊性 (perp funding, MEV, on-chain)",
        f"{config.INITIAL_TOPIC} — 即時 tick anomaly detection",
        f"{config.INITIAL_TOPIC} — Arxiv recent papers scan",
    ]
    for s in seeds:
        await kb_sql.add_task(s, description="seeded from INITIAL_TOPIC")
    event("seeded", n=len(seeds))


async def run():
    await kb_sql.init()
    await _seed_initial()
    logger.info("research loop started")
    event("loop_started")
    while True:
        try:
            task = await kb_sql.next_task()
            if not task:
                await asyncio.sleep(15)
                continue
            CURRENT.update(task_id=task["id"], topic=task["topic"],
                           started=asyncio.get_event_loop().time())
            event("task_start", id=task["id"], topic=task["topic"])
            await MUX.send({"type": "task_start", "id": task["id"], "topic": task["topic"]})

            # Triangulation tasks short-circuit the planner — we already know
            # exactly what we want to do (verify a known finding) and the
            # planner-LLM otherwise generates an ask_user with the bare topic
            # id, which is meaningless to the user. See triangulation_ask.py.
            if (task.get("topic") or "").startswith("[triangulate]"):
                try:
                    out = await _triangulate_finding(task)
                    summary = out.get("summary") or "triangulation completed"
                except Exception as e:
                    logger.warning(f"triangulate failed: {e}")
                    summary = f"triangulate error: {e}"
                await kb_sql.complete_task(task["id"], result=summary)
                event("task_done", id=task["id"], triangulated=True)
                await MUX.send({"type": "finding", "msg": summary,
                                "topic": task["topic"]})
                continue

            subs = await plan(task["topic"])
            results: list[dict] = []
            for sub in subs:
                flag, reason = intr.peek_interrupt()
                if flag:
                    event("interrupt_handled", reason=reason)
                    intr.clear_interrupt()
                    await MUX.send({"type": "interrupted", "reason": reason})
                    break
                try:
                    r = await execute(sub, parent_topic=task["topic"])
                    results.append(r)
                except Exception as e:
                    logger.warning(f"subtask failed: {e}")
                    results.append({"title": sub.get("title"), "answer": f"error: {e}"})
            synth = await synthesize(task["topic"], results)
            await MUX.send({"type": "finding", "msg": synth.get("finding"),
                            "topic": task["topic"]})
            followups = await reflect(task["topic"], synth.get("finding", ""))
            for ft in followups:
                await kb_sql.add_task(ft, description="follow-up", parent_id=task["id"])
            await kb_sql.complete_task(task["id"], result=synth.get("finding", ""))
            event("task_done", id=task["id"], followups=len(followups))
        except Exception as e:
            logger.exception(f"loop iter failed: {e}")
            event("loop_error", error=repr(e))
            await asyncio.sleep(5)
