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
from . import strategy_synth


import os
TASK_TIMEOUT_S = int(os.getenv("PONDR_TASK_TIMEOUT_S", "600"))  # 10 min default

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




async def _run_task(task):
    """Per-task body, called with asyncio.wait_for timeout in run()."""
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
        return

    if (task.get("topic") or "").startswith(strategy_synth.STRATEGY_TASK_PREFIX):
        try:
            out = await strategy_synth.run(task)
            summary = out.get("summary") or "strategy synthesis completed"
        except Exception as e:
            logger.exception(f"strategy_synth failed: {e}")
            summary = f"strategy_synth error: {e}"
        await kb_sql.complete_task(task["id"], result=summary)
        event("task_done", id=task["id"], strategy_synth=True)
        await MUX.send({"type": "finding", "msg": summary,
                        "topic": task["topic"]})
        return

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

            try:
                await asyncio.wait_for(_run_task(task), timeout=TASK_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning(f"task #{task['id']} timed out after {TASK_TIMEOUT_S}s — cancelling")
                event("task_timeout", id=task["id"], topic=task["topic"], timeout_s=TASK_TIMEOUT_S)
                await kb_sql.complete_task(task["id"], result=f"[timeout after {TASK_TIMEOUT_S}s]")
                # mark as cancelled instead of done so it doesn't pollute results
                # (complete_task sets status='done'; override:)
                import aiosqlite
                async with aiosqlite.connect(config.DB_KB) as db:
                    await db.execute("UPDATE tasks SET status='timeout' WHERE id=?", (task["id"],))
                    await db.commit()
                await MUX.send({"type": "task_timeout", "id": task["id"], "topic": task["topic"]})
        except Exception as e:
            logger.exception(f"loop iter failed: {e}")
            event("loop_error", error=repr(e))
            await asyncio.sleep(5)
