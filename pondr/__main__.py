"""pondr orchestrator — boots all concurrent asyncio services."""
from __future__ import annotations
import asyncio
import signal
import sys
import threading

from . import config
from .utils.log import logger, event
from .kb import sqlite as kb_sql, duckdb as ddb, chroma
from .feeds import (ALL as FEED_CLASSES,
                    DEPTH_ALL as DEPTH_FEED_CLASSES,
                    AUX_ALL as AUX_FEED_CLASSES)
from .polls import coingecko, fred, news_rss
from .server.channels import build_channels, MUX
from .server import dashboard
from .server.interrupt import set_interrupt
from .research import loop as research_loop
from . import llm
import json

from . import runtime  # FEEDS lives here so dashboard sees same list



async def _kmap_run():
    from .research import knowledge_map
    await knowledge_map.run_periodic()


async def _curriculum_run():
    from .research import curriculum
    await curriculum.run_periodic()


async def _llm_stats_publisher():
    """Push fresh LLM stats to dashboard listeners every ~30s."""
    from .kb import llm_stats
    from .server import event_bus
    while True:
        try:
            stats = await llm_stats.get_stats(window_hours=24)
            event_bus.publish("llm_stats_updated", {"stats": stats})
        except Exception as e:
            logger.debug(f"llm_stats publisher: {e}")
        await asyncio.sleep(30)


def _format_rag_chunks(chunks: list[dict], max_chars: int = 800) -> str:
    out = []
    used = 0
    for c in chunks or []:
        doc = (c.get("doc") or "").strip()
        if not doc:
            continue
        snippet = doc[:240].replace("\n", " ")
        line = f"- (id={c.get('id','')[:8]}) {snippet}"
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out) or "(none)"


def _format_curriculum_summary(tree_rows: list[dict], max_lines: int = 25,
                               max_chars: int | None = None) -> str:
    """Flatten the curriculum tree into 'title (status, mastery%)' lines.

    If max_chars is set, truncate the joined output (keeping whole lines).
    """
    lines: list[dict] = []

    def _walk(nodes: list[dict], depth: int = 0):
        for n in nodes:
            if len(lines) >= max_lines:
                return
            indent = "  " * depth
            mastery = n.get("mastery_pct") or 0.0
            status = n.get("status") or "open"
            title = (n.get("title") or "").strip()[:80]
            lines.append({"line": f"{indent}- {title} ({status}, "
                                  f"mastery {mastery:.0f}%)"})
            _walk(n.get("children") or [], depth + 1)

    _walk(tree_rows or [])
    if not lines:
        return "(no curriculum yet)"
    out: list[str] = []
    used = 0
    for ln in (l["line"] for l in lines):
        if max_chars is not None and used + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def _format_recent_findings(notes: list[dict], max_n: int = 8) -> str:
    out = []
    for n in (notes or [])[:max_n]:
        c = n.get("content") or ""
        conf = n.get("confidence")
        suffix = f" (conf {conf:.2f})" if isinstance(conf, (int, float)) else ""
        out.append(f"- [{(n.get('topic') or '')[:50]}] "
                   f"{c[:200]}{suffix}")
    return "\n".join(out) or "(no recent findings)"


async def _build_chat_answer(user_text: str) -> str:
    """Run a second LLM call grounded in RAG / curriculum / recent findings."""
    from .kb import chroma as chroma_kb
    from .kb import curriculum as curr_kb
    from .kb import sqlite as kb_sql_inner

    rag_chunks: list[dict] = []
    curr_tree: list[dict] = []
    recent_notes: list[dict] = []
    try:
        # k=3 (down from 5) to keep prompt small
        rag_chunks = await chroma_kb.search(user_text, k=3)
    except Exception as e:
        logger.debug(f"rag_search failed in chat answer: {e}")
    try:
        curr_tree = await curr_kb.tree()
    except Exception as e:
        logger.debug(f"curriculum tree failed in chat answer: {e}")
    try:
        # 5 most recent (down from 10)
        recent_notes = await kb_sql_inner.find_notes_by_topic(
            "finding/", limit=5)
    except Exception as e:
        logger.debug(f"recent findings failed in chat answer: {e}")

    context_msg = (
        f"User question: {user_text}\n\n"
        f"Relevant KB chunks (RAG):\n"
        f"{_format_rag_chunks(rag_chunks)}\n\n"
        f"Current curriculum (topics & mastery):\n"
        f"{_format_curriculum_summary(curr_tree, max_chars=500)}\n\n"
        f"Recent findings:\n"
        f"{_format_recent_findings(recent_notes, max_n=5)}\n\n"
        f"Reply concisely in the user's preferred language. Cite source ids "
        f"when claiming facts. If you don't have enough information, say so "
        f"explicitly and mention what's still unknown."
    )
    sys_msg = (
        ""
        "Answer the user's chat using the supplied KB context. Be honest about "
        "what you don't know. Keep replies under ~250 words unless asked for more."
    )
    # User-facing reply — pass language hint so the LLM stays in 繁中 etc.
    try:
        from .kb import preferences as prefs_kb
        user_lang = await prefs_kb.get_active_language()
    except Exception:
        user_lang = None
    reply_resp = await llm.chat(
        [
            {"role": "system", "content": sys_msg},
            {"role": "user", "content": context_msg},
        ],
        temperature=0.3, max_tokens=400, language_hint=user_lang,
    )
    return llm.assistant_text(reply_resp).strip()


async def _handle_feature_request(obj: dict, txt: str) -> dict:
    """Convert a router 'feature_request' decision into a capability_gap row.

    Returns the dict that should be sent on MUX.
    Pulled out as a helper so it can be unit-tested without spinning up the
    channel dispatch loop.
    """
    capability = (obj.get("capability") or "").strip() or "(unspecified)"
    why = (obj.get("why_needed") or txt).strip()
    try:
        severity = int(obj.get("severity", 3))
    except (TypeError, ValueError):
        severity = 3
    severity = max(1, min(5, severity))
    suggested = obj.get("suggested_solution")
    from .kb import capability_gaps as cg_kb
    out = await cg_kb.report(capability, why, severity, suggested)
    row = out.get("row") or {}
    gap_id = row.get("id")
    ack = (obj.get("reply") or
           f"收到，已記錄為 capability gap "
           f"#{gap_id}: {capability}. "
           f"我會在後續 task 中考慮處理。")
    return {
        "type": "feature_acknowledged",
        "capability": capability,
        "gap_id": gap_id,
        "severity": severity,
        "created": out.get("created"),
        "msg": ack,
    }


async def _knowledge_query_reply(topic_query: str) -> str:
    """Build a 'what do you know about X' reply combining notes + RAG + gaps."""
    from .kb import sqlite as kb_sql, knowledge_gaps as kg_kb, chroma
    notes = await kb_sql.find_notes_by_topic(f"finding/{topic_query[:60]}", limit=8)
    rag = await chroma.search(topic_query, k=5)
    gaps = await kg_kb.list_by_topic(topic=topic_query)
    known = [g for g in gaps if g["status"] == "known"][:6]
    unknown = [g for g in gaps if g["status"] == "unknown"][:6]
    parts = [f"## What I know about {topic_query!r}"]
    if notes:
        parts.append("\n### Findings:")
        for n in notes[:5]:
            c = f" (conf {n.get('confidence',0):.2f})" if n.get('confidence') is not None else ""
            parts.append(f"- {n['content'][:200]}{c}")
    if rag:
        parts.append("\n### Related memory:")
        for r in rag[:3]:
            parts.append(f"- {(r.get('doc') or '')[:160]}")
    if known:
        parts.append("\n### Known sub-questions:")
        for g in known:
            parts.append(f"- ✓ {g['sub_question']} → {(g.get('answer_summary') or '')[:160]}")
    if unknown:
        parts.append("\n### ⚠️ I don't know:")
        for g in unknown:
            parts.append(f"- ❓ {g['sub_question']}")
    if not (notes or rag or known or unknown):
        parts.append("(no recorded knowledge yet — try /topic " + topic_query + ")")
    return "\n".join(parts)

async def _channel_dispatch():
    """Route inbound channel messages: chat → LLM router; explicit types handled here."""
    from .tools.ask import ask_user as _aside  # noqa
    async for msg in MUX.messages():
        try:
            t = msg.get("type", "chat")
            if t == "answer":
                # already resolved by mux reader; nothing to do
                continue
            if t == "add_topic":
                topic = (msg.get("topic") or "").strip()
                if topic:
                    tid = await kb_sql.add_task(topic, description="user via channel")
                    await MUX.send({"type": "queued", "id": tid, "topic": topic})
                continue
            if t == "status":
                await MUX.send({"type": "status",
                                "current": research_loop.CURRENT,
                                "queued": await kb_sql.list_tasks(status="queued", limit=10)})
                continue
            if t == "chat":
                txt = (msg.get("text") or "").strip()
                if not txt:
                    continue
                # quick LLM router: interrupt vs queue topic vs answer
                router_resp = await llm.chat([
                    {"role": "system",
                     "content": ("intent router. classify the user's "
                                 "chat into JSON. Possible actions: "
                                 "'interrupt' (stop current research task), "
                                 "'queue_topic' (add a research topic), "
                                 "'save_preference' (user gives a long-lived "
                                 "instruction like '請以後 / always / from now on / "
                                 "別再 / never'; capture key+value+category — "
                                 "categories: communication/workflow/notification/research; "
                                 "NEVER store secrets/keys/PII), "
                                 "'know_about' (user is asking 'what do you know about X' / "
                                 "'你知道什麼關於 X'; capture target topic), "
                                 "'feature_request' (user suggests pondr should "
                                 "ADD/SUPPORT a new capability — e.g. 'should also "
                                 "subscribe to kraken WS', '應該接 binance aggTrades', "
                                 "'add coinbase ticker feed', 'support futures'. "
                                 "Capture: capability (short slug like "
                                 "'feed:kraken-ws-trade' or 'connector:bybit-futures'), "
                                 "why_needed (1-line reason), severity (1-5, default 3, "
                                 "raise to 4-5 only if user is blocked), "
                                 "suggested_solution (optional implementation hint)), "
                                 "'answer' (a normal chat reply). "
                                 "Output JSON: {action, topic?, reply?, "
                                 "pref_key?, pref_value?, pref_category?, "
                                 "knowledge_topic?, capability?, why_needed?, "
                                 "severity?, suggested_solution?}. "
                                 "Be conservative with save_preference — only when "
                                 "the user clearly intends a persistent instruction. "
                                 "Use feature_request when the user is clearly asking "
                                 "pondr to gain a new technical capability, not for "
                                 "casual mentions.")},
                    {"role": "user", "content": txt},
                ], temperature=0.0, max_tokens=300)
                rt = llm.assistant_text(router_resp)
                obj: dict = {}
                try:
                    i, j = rt.find("{"), rt.rfind("}")
                    if i >= 0 and j > i:
                        obj = json.loads(rt[i:j + 1])
                except Exception:
                    obj = {"action": "answer", "reply": rt[:300]}
                action = obj.get("action") or "answer"
                if action == "interrupt":
                    set_interrupt(reason=txt[:200])
                    await MUX.send({"type": "ack", "msg": "interrupt queued"})
                elif action == "queue_topic":
                    topic = obj.get("topic") or txt
                    tid = await kb_sql.add_task(topic, description="user via chat")
                    await MUX.send({"type": "queued", "id": tid, "topic": topic})
                elif action == "save_preference":
                    pkey = (obj.get("pref_key") or "").strip()
                    pval = (obj.get("pref_value") or "").strip()
                    pcat = obj.get("pref_category")
                    from .kb import preferences as prefs_kb
                    if pkey and pval:
                        out = await prefs_kb.save(pkey, pval, category=pcat,
                                                  channel=msg.get("channel", "chat"),
                                                  user_msg=txt)
                        await MUX.send({"type": "preference_saved",
                                        "key": pkey, "value": pval,
                                        "ok": out.get("ok"),
                                        "msg": obj.get("reply") or "(preference noted)"})
                    else:
                        await MUX.send({"type": "reply",
                                        "msg": "tried to save preference but key/value missing"})
                elif action == "know_about":
                    target = (obj.get("knowledge_topic") or txt).strip()
                    reply = await _knowledge_query_reply(target)
                    await MUX.send({"type": "knowledge_reply",
                                    "topic": target, "msg": reply})
                elif action == "feature_request":
                    await MUX.send(await _handle_feature_request(obj, txt))
                else:
                    # action == 'answer' — run a second LLM call grounded in
                    # RAG / curriculum / recent findings, instead of replying
                    # with the router's (often empty) reply field.
                    try:
                        reply = await _build_chat_answer(txt)
                    except Exception as e:
                        logger.warning(f"chat answer build failed: {e}")
                        reply = ""
                    if not reply:
                        # Fall back to whatever the router emitted; if that's
                        # also blank, give the user a useful diagnostic instead
                        # of the previous opaque "(noted)".
                        reply = (obj.get("reply") or
                                 "(empty reply from LLM — please retry)")
                    await MUX.send({"type": "reply", "msg": reply})
        except Exception as e:
            logger.warning(f"channel dispatch: {e}")


async def _start_dashboard():
    """Run uvicorn in a background thread (blocking server)."""
    def _serve():
        try:
            dashboard.serve()
        except Exception as e:
            logger.exception(f"dashboard crashed: {e}")
    t = threading.Thread(target=_serve, daemon=True, name="dashboard")
    t.start()
    logger.info(f"dashboard thread started on http://127.0.0.1:{config.DASHBOARD_PORT}")
    return t



async def _print_startup_pending_summary():
    """Print/notify pending questions surviving from a previous run."""
    from .kb import questions as q_kb
    try:
        rows = await q_kb.list_pending()
    except Exception as e:
        logger.warning(f"pending summary: {e}")
        return
    if not rows:
        return
    logger.info(f"[startup] {len(rows)} pending questions awaiting answer:")
    for q in rows[:20]:
        age_s = q.get("age_seconds") or 0
        if age_s > 3600:
            age = f"{age_s/3600:.1f}h"
        elif age_s > 60:
            age = f"{age_s/60:.0f}m"
        else:
            age = f"{age_s:.0f}s"
        logger.info(f"  [{q['qid'][:8]}] (asked {age} ago) {q['question'][:120]}")
    # Push a digest msg too
    try:
        await MUX.send({
            "type": "startup_pending_digest",
            "count": len(rows),
            "questions": [{"qid": q["qid"], "question": q["question"],
                           "age_seconds": q.get("age_seconds"),
                           "asked_by": q.get("asked_by")} for q in rows[:10]],
        })
    except Exception:
        pass

async def main_async():
    logger.info("pondr booting…")
    event("boot", config={"llm_model": config.LLM_MODEL,
                          "channels": config.CHANNELS_ENABLED})
    await kb_sql.init()
    await ddb.init()
    await chroma.init()
    from .kb import preferences as prefs_kb, questions as q_kb, capability_gaps as cap_kb, knowledge_gaps as kg_kb, curriculum as curr_kb, strategies as strat_kb
    await q_kb.init()
    await prefs_kb.init()
    await cap_kb.init()
    await kg_kb.init()
    await curr_kb.init()
    await strat_kb.init()

    # feeds (trade)
    runtime.FEEDS[:] = [cls() for cls in FEED_CLASSES]
    # feeds (depth) — orderbook-imbalance pipeline
    runtime.DEPTH_FEEDS[:] = [cls() for cls in DEPTH_FEED_CLASSES]
    # feeds (auxiliary: aggTrade + depth-diff) — persisted to DuckDB only
    runtime.AUX_FEEDS[:] = [cls() for cls in AUX_FEED_CLASSES]

    # quant scanners (arb + orderbook imbalance)
    from .quant.arb.scanner import ArbScanner
    from .quant.orderbook.imbalance import ImbalanceDetector
    runtime.ARB_SCANNER = ArbScanner()
    runtime.IMBALANCE_DETECTOR = ImbalanceDetector()

    # channels
    build_channels()
    await MUX.start_all()

    # dashboard
    await _start_dashboard()

    # startup pending-Q summary
    await _print_startup_pending_summary()

    # background tasks
    bg = [
        asyncio.create_task(_channel_dispatch(), name="channel-dispatch"),
        asyncio.create_task(research_loop.run(), name="research-loop"),
        asyncio.create_task(coingecko.run(), name="poll-coingecko"),
        asyncio.create_task(fred.run(), name="poll-fred"),
        asyncio.create_task(news_rss.run(), name="poll-rss"),
        asyncio.create_task(_kmap_run(), name="knowledge-map"),
        asyncio.create_task(_curriculum_run(), name="curriculum"),
        asyncio.create_task(_llm_stats_publisher(), name="llm-stats-pub"),
        asyncio.create_task(runtime.ARB_SCANNER.run(), name="arb-scanner"),
        asyncio.create_task(runtime.IMBALANCE_DETECTOR.run(),
                            name="orderbook-imbalance"),
    ]
    for f in runtime.FEEDS:
        bg.append(asyncio.create_task(f.run(), name=f"feed-{f.name}"))
    for f in runtime.DEPTH_FEEDS:
        bg.append(asyncio.create_task(f.run(), name=f"depth-{f.name}"))
    for f in runtime.AUX_FEEDS:
        bg.append(asyncio.create_task(f.run(), name=f"aux-{f.label}"))

    # heartbeat
    async def _heartbeat():
        from .server import event_bus
        while True:
            # Periodic tick_count snapshot for the SSE stream — coarser
            # than per-tick (every heartbeat = 30s) to keep the wire quiet.
            try:
                feed_payload = {
                    "trade": [{"name": f.name, "tick_count": f.tick_count,
                               "connected": f.connected,
                               "last_tick": f.last_tick}
                              for f in runtime.FEEDS],
                    "depth": [{"name": getattr(f, "label", f.name),
                               "tick_count": getattr(f, "msg_count", 0),
                               "connected": f.connected,
                               "last_tick": getattr(f, "last_msg_ts", 0.0)}
                              for f in runtime.DEPTH_FEEDS],
                    "aux": [{"name": getattr(f, "label", f.name),
                             "tick_count": getattr(f, "msg_count", 0),
                             "connected": f.connected,
                             "last_tick": getattr(f, "last_msg_ts", 0.0)}
                            for f in runtime.AUX_FEEDS],
                }
                event_bus.publish("tick_count_update", feed_payload)
            except Exception:
                pass
            event("heartbeat",
                  current=research_loop.CURRENT,
                  feeds=[(f.name, f.tick_count) for f in runtime.FEEDS],
                  depth_feeds=[(f.label, f.msg_count)
                               for f in runtime.DEPTH_FEEDS],
                  aux_feeds=[(f.label, f.msg_count)
                             for f in runtime.AUX_FEEDS],
                  arb_scans=getattr(runtime.ARB_SCANNER, "scans", 0),
                  arb_opps=getattr(runtime.ARB_SCANNER, "opportunities", 0),
                  ob_samples=getattr(runtime.IMBALANCE_DETECTOR,
                                     "samples_persisted", 0),
                  ob_alerts=getattr(runtime.IMBALANCE_DETECTOR,
                                    "alerts_fired", 0))
            await asyncio.sleep(30)
    bg.append(asyncio.create_task(_heartbeat(), name="heartbeat"))

    # graceful shutdown
    stop_evt = asyncio.Event()

    def _sig(*_):
        logger.info("signal received, shutting down")
        stop_evt.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig)
        except NotImplementedError:
            pass

    await stop_evt.wait()
    for t in bg:
        t.cancel()
    for f in runtime.FEEDS:
        await f.stop()
    for f in runtime.DEPTH_FEEDS:
        await f.stop()
    for f in runtime.AUX_FEEDS:
        await f.stop()
    if runtime.ARB_SCANNER:
        runtime.ARB_SCANNER.stop()
    if runtime.IMBALANCE_DETECTOR:
        runtime.IMBALANCE_DETECTOR.stop()
    await MUX.stop_all()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
