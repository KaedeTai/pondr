"""FastAPI dashboard. /api/state JSON, /ws/state push, embedded UI."""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import config
from ..kb import (sqlite as kb_sql, duckdb as ddb, chroma, questions as q_kb,
                  preferences as prefs_kb, capability_gaps as cap_kb,
                  knowledge_gaps as kg_kb, curriculum as curr_kb,
                  llm_stats as llm_stats_kb)
from ..server.channels import MUX, list_questions, resolve_question
from ..server import event_bus
from ..research import CURRENT
from ..utils import llm_log
from ..utils.log import recent_events, event

START_TS = time.time()
TPL_DIR = Path(__file__).parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(TPL_DIR)),
                   autoescape=select_autoescape(["html"]),
                   auto_reload=True)
# Note: we DO NOT cache the template at import time; auto_reload only works when
# get_template() is called per-request (it stat()s the file and re-reads on mtime change).
def _get_tpl():
    return _env.get_template("dashboard.html")

app = FastAPI(title="pondr dashboard")


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except FileNotFoundError:
        return 0


async def _state() -> dict:
    counts = await kb_sql.counts()
    tick_count = await ddb.count()
    try:
        aggtrade_count = await ddb.count_aggtrades()
    except Exception:
        aggtrade_count = 0
    try:
        depth_diff_count = await ddb.count_depth_diffs()
    except Exception:
        depth_diff_count = 0
    chroma_count = await chroma.count()
    feeds = []
    try:
        from ..runtime import FEEDS, DEPTH_FEEDS, AUX_FEEDS
        for f in FEEDS:
            feeds.append({"name": f.name, "connected": f.connected,
                          "tick_count": f.tick_count,
                          "last_tick": f.last_tick,
                          "kind": "trade"})
        for f in DEPTH_FEEDS:
            feeds.append({"name": getattr(f, "label", f.name),
                          "connected": f.connected,
                          "tick_count": getattr(f, "msg_count", 0),
                          "last_tick": getattr(f, "last_msg_ts", 0.0),
                          "kind": "depth"})
        for f in AUX_FEEDS:
            feeds.append({"name": getattr(f, "label", f.name),
                          "connected": f.connected,
                          "tick_count": getattr(f, "msg_count", 0),
                          "last_tick": getattr(f, "last_msg_ts", 0.0),
                          "kind": "aux"})
    except Exception:
        pass
    active = await kb_sql.list_tasks(status="running", limit=10)
    queued = await kb_sql.list_tasks(status="queued", limit=20)
    done = await kb_sql.list_tasks(status="done", limit=10)
    return {
        "now": time.time(),
        "uptime_s": time.time() - START_TS,
        "current_task": CURRENT,
        "channels": [{"name": ch.name, "connected": ch.connected}
                     for ch in MUX.channels],
        "questions": await q_kb.list_pending(),
        "preferences": await prefs_kb.list_active(),
        "capability_gaps": await cap_kb.list_open(),
        "recent_findings": await kb_sql.find_notes_by_topic("finding/", limit=20),
        "recent_backtests": await _recent_backtests(10),
        "recent_arbs": await _recent_arbs(10),
        "recent_imbalances": await _recent_imbalances(10),

        "knowledge_map": await kg_kb.tree(),
        "knowledge_counts": await kg_kb.counts_by_status(),
        "curriculum": await curr_kb.tree(),
        "curriculum_overall_mastery": await curr_kb.overall_mastery(),
        "curriculum_counts": await curr_kb.counts_by_status(),
        "kb_counts": counts,
        "ticks_total": tick_count,
        "aggtrades_total": aggtrade_count,
        "depth_diffs_total": depth_diff_count,
        "chroma_chunks": chroma_count,
        "db_kb_bytes": _file_size(config.DB_KB),
        "db_ticks_bytes": _file_size(config.DB_TICKS),
        "feeds": feeds,
        "tasks_running": active,
        "tasks_queued": queued,
        "tasks_done": done,
        "events": recent_events(50),
        "llm_recent": llm_log.recent(50),
        "llm_stats": await llm_stats_kb.get_stats(window_hours=24),
    }


@app.get("/", response_class=HTMLResponse)
async def index(_: Request):
    return _get_tpl().render(initial_topic=config.INITIAL_TOPIC,
                       ws_port=config.WS_PORT,
                       dash_port=config.DASHBOARD_PORT)


@app.get("/api/state")
async def api_state():
    return JSONResponse(await _state())


@app.get("/api/llm_stats")
async def api_llm_stats(window: str = "24h"):
    """Window e.g. '1h', '24h', '7d'. Defaults to 24h."""
    w = (window or "").strip().lower()
    if w.endswith("h"):
        try:
            hrs = int(w[:-1])
        except Exception:
            hrs = 24
    elif w.endswith("d"):
        try:
            hrs = int(w[:-1]) * 24
        except Exception:
            hrs = 24
    else:
        hrs = 24
    return await llm_stats_kb.get_stats(window_hours=max(1, hrs))


@app.post("/api/answer")
async def api_answer(payload: dict):
    qid = payload.get("qid")
    answer = payload.get("answer", "")
    if not qid:
        return {"ok": False, "error": "missing qid"}
    ok = await resolve_question(qid, answer, via="dashboard")
    return {"ok": ok}


@app.post("/api/topic")
async def api_topic(payload: dict):
    topic = (payload.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "empty topic"}
    tid = await kb_sql.add_task(topic, description="user-submitted via dashboard")
    event("user_topic", topic=topic, id=tid)
    return {"ok": True, "id": tid}


@app.get("/api/stream")
async def api_stream(_: Request):
    """Server-Sent Events stream of incremental dashboard updates.

    Frame protocol (each frame is JSON in the SSE ``data:`` field):

      {"type": "snapshot", "state": <full /api/state body>}    — initial frame
      {"type": "<event-type>", "ts": ..., "payload": {...}}    — incremental
      {"type": "ping"}                                         — keepalive

    Event types are documented in :mod:`pondr.server.event_bus` (see
    ``KNOWN_EVENT_TYPES``). The Vue client patches its reactive store on
    each frame; on disconnect it falls back to polling ``/api/state``.
    """
    bus_q = event_bus.subscribe()
    llm_q = llm_log.subscribe()

    async def gen():
        try:
            # Initial snapshot — same payload as /api/state, lets the client
            # boot without an extra request.
            snap = await _state()
            yield f"data: {json.dumps({'type':'snapshot','state':snap}, default=str)}\n\n"
            while True:
                # Race the two upstream queues + a 25s keepalive timeout.
                bus_task = asyncio.create_task(bus_q.get())
                llm_task = asyncio.create_task(llm_q.get())
                try:
                    done, pending = await asyncio.wait(
                        {bus_task, llm_task},
                        timeout=25.0,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        # keepalive
                        for t in pending:
                            t.cancel()
                        yield f"data: {json.dumps({'type':'ping'})}\n\n"
                        continue
                    for t in done:
                        rec = t.result()
                        if t is llm_task:
                            frame = {
                                "type": "llm_io_logged",
                                "ts": time.time(),
                                "payload": {"rec": rec},
                            }
                        else:
                            frame = rec
                        yield (f"data: {json.dumps(frame, default=str)}\n\n")
                    for t in pending:
                        t.cancel()
                except asyncio.CancelledError:
                    bus_task.cancel(); llm_task.cancel()
                    raise
        finally:
            event_bus.unsubscribe(bus_q)
            llm_log.unsubscribe(llm_q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.websocket("/ws/state")
async def ws_state(ws: WebSocket):
    await ws.accept()
    q = llm_log.subscribe()
    try:
        await ws.send_text(json.dumps(await _state(), default=str))
        while True:
            try:
                rec = await asyncio.wait_for(q.get(), timeout=2.0)
                await ws.send_text(json.dumps({"type": "llm", "rec": rec}, default=str))
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "state",
                                               "state": await _state()}, default=str))
    except WebSocketDisconnect:
        pass
    finally:
        llm_log.unsubscribe(q)


@app.get("/api/capability_gaps")
async def api_capgaps():
    return {"capability_gaps": await cap_kb.list_open()}


@app.post("/api/capability_gaps/{cap_id}/status")
async def api_capgap_status(cap_id: int, payload: dict):
    status = payload.get("status", "")
    notes = payload.get("notes")
    return {"ok": await cap_kb.update_status(cap_id, status, notes)}


@app.get("/api/prefs")
async def api_prefs_list():
    return {"preferences": await prefs_kb.list_active()}


@app.post("/api/prefs")
async def api_prefs_save(payload: dict):
    key = (payload.get("key") or "").strip()
    value = (payload.get("value") or "").strip()
    category = payload.get("category")
    if not key or not value:
        return {"ok": False, "error": "missing key/value"}
    return await prefs_kb.save(key, value, category=category,
                               channel="dashboard", user_msg=payload.get("note"))


@app.delete("/api/prefs/{key}")
async def api_prefs_delete(key: str):
    return {"ok": await prefs_kb.delete(key, channel="dashboard")}


@app.get("/api/knowledge")
async def api_knowledge(topic: str | None = None):
    if topic:
        return {"rows": await kg_kb.list_by_topic(topic=topic)}
    return {"tree": await kg_kb.tree(), "counts": await kg_kb.counts_by_status()}


@app.post("/api/knowledge/{gap_id}/status")
async def api_kg_status(gap_id: int, payload: dict):
    return {"ok": await kg_kb.mark_status(
        gap_id, payload.get("status", ""),
        answer_summary=payload.get("answer_summary"),
        sources=payload.get("sources"))}



async def _recent_backtests(limit: int = 10) -> list[dict]:
    import aiosqlite, json
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, strategy, symbol, n_ticks, metrics_json, ascii_curve, "
            "created_at, confidence FROM backtests ORDER BY id DESC LIMIT ?",
            (limit,))
        rows = await cur.fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
        except Exception:
            d["metrics"] = {}
        out.append(d)
    return out


async def _recent_arbs(limit: int = 10) -> list[dict]:
    import aiosqlite
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM arb_opportunities ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def _recent_imbalances(limit: int = 10) -> list[dict]:
    try:
        rows = await ddb.query(
            f"SELECT * FROM orderbook_imbalances ORDER BY ts DESC LIMIT {int(limit)}")
        return rows
    except Exception:
        return []


@app.get("/api/backtests")
async def api_backtests():
    return {"backtests": await _recent_backtests(50)}


@app.get("/api/backtests/{bt_id}")
async def api_backtest_detail(bt_id: int):
    import aiosqlite, json
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM backtests WHERE id=?", (bt_id,))
        r = await cur.fetchone()
    if not r:
        return {"error": "not found"}
    d = dict(r)
    try:
        d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
        d["equity"] = json.loads(d.pop("equity_blob") or "{}")
    except Exception:
        pass
    return d


# ---- Curriculum ("📚 What I've learned") --------------------------------

@app.get("/api/curriculum")
async def api_curriculum_tree():
    return {
        "overall_mastery_pct": round(await curr_kb.overall_mastery(), 1),
        "counts": await curr_kb.counts_by_status(),
        "tree": await curr_kb.tree(),
    }


@app.get("/api/curriculum/{node_id}")
async def api_curriculum_node(node_id: int):
    node = await curr_kb.get_node(node_id)
    if not node:
        return JSONResponse({"error": "not found"}, status_code=404)
    # Hydrate referenced rows for the drill-down modal
    notes_full: list[dict] = []
    bts_full: list[dict] = []
    gaps_full: list[dict] = []
    import aiosqlite, json as _json
    async with aiosqlite.connect(config.DB_KB) as db:
        db.row_factory = aiosqlite.Row
        if node.get("note_ids"):
            qm = ",".join("?" for _ in node["note_ids"])
            cur = await db.execute(
                f"SELECT id, topic, content, confidence, created_at "
                f"FROM notes WHERE id IN ({qm}) ORDER BY id DESC",
                node["note_ids"])
            notes_full = [dict(r) for r in await cur.fetchall()]
        if node.get("related_backtest_ids"):
            qm = ",".join("?" for _ in node["related_backtest_ids"])
            cur = await db.execute(
                f"SELECT id, strategy, symbol, n_ticks, metrics_json, "
                f"ascii_curve, confidence FROM backtests WHERE id IN ({qm}) "
                f"ORDER BY id DESC",
                node["related_backtest_ids"])
            for r in await cur.fetchall():
                d = dict(r)
                try:
                    d["metrics"] = _json.loads(d.pop("metrics_json") or "{}")
                except Exception:
                    d["metrics"] = {}
                bts_full.append(d)
        if node.get("related_gap_ids"):
            qm = ",".join("?" for _ in node["related_gap_ids"])
            cur = await db.execute(
                f"SELECT id, topic, sub_question, status, answer_summary "
                f"FROM knowledge_gaps WHERE id IN ({qm}) ORDER BY id DESC",
                node["related_gap_ids"])
            gaps_full = [dict(r) for r in await cur.fetchall()]
    return {**node, "notes_full": notes_full,
            "backtests_full": bts_full, "gaps_full": gaps_full}


@app.post("/api/curriculum/regenerate")
async def api_curriculum_regenerate(payload: dict | None = None):
    from ..research import curriculum as curr_gen
    out = await curr_gen.regenerate(force=bool((payload or {}).get("force")))
    return out


@app.post("/api/curriculum/{node_id}/deep_dive")
async def api_curriculum_deep_dive(node_id: int):
    from ..tools.curriculum import curriculum_deep_dive
    return await curriculum_deep_dive(node_id)


def serve():
    import uvicorn
    uvicorn.run(app, host=config.BIND_HOST, port=config.DASHBOARD_PORT, log_level="warning")
