"""Triangulation helper — verify low-confidence findings.

When the synthesizer enqueues a `[triangulate]` task for a finding with
confidence < LOW_CONFIDENCE, the research loop hands it to `triangulate(task)`
in this module instead of running it through the normal plan→execute→synth
pipeline. This avoids the planner-LLM generating an `ask_user` subtask whose
question contains only the internal topic id (e.g. ``unique-topic-1aa409``)
with no actual content for the human to verify.

The user is shown the actual finding text + sources + 3 options:
    1) 提供資料 (用 reply 接著傳)         → captured as additional context
    2) 我不知道，你自己研究                → triggers `triangulate_self` (auto)
    3) 這 finding 不重要，跳過             → finding marked archived
The auto path web/rag-searches for the claim, re-scores confidence, updates
the underlying finding note.
"""
from __future__ import annotations
import asyncio
import json
import re
import time
from typing import Optional

import aiosqlite

from .. import config, llm
from ..kb import sqlite as kb_sql
from ..server.channels import ask_user
from ..tools.rag import rag_search as _rag_search
from ..tools.web_search import web_search as _web_search
from ..utils.log import event, logger


# ----- option labels (user-facing strings; also returned by the channel) -----
OPTION_PROVIDE = "提供資料 (用 reply 接著傳)"
OPTION_AUTO = "我不知道，你自己研究"
OPTION_SKIP = "這 finding 不重要，跳過"

_TRI_PREFIX = "[triangulate] "
_TRI_SUFFIX = " — verify low-confidence finding"

# Parse the description format synthesizer.py writes:
#   "Original finding (conf 0.30): <finding text>\nReason: <reason>\nFind ≥2 ..."
_FINDING_RE = re.compile(
    r"Original finding \(conf ([\d.]+)\):\s*(.+?)\nReason:\s*(.+?)\n",
    re.DOTALL,
)


def _parse_triangulate_task(task: dict) -> dict:
    topic = task.get("topic") or ""
    desc = task.get("description") or ""
    parent_topic = topic
    if topic.startswith(_TRI_PREFIX):
        parent_topic = topic[len(_TRI_PREFIX):]
        if parent_topic.endswith(_TRI_SUFFIX):
            parent_topic = parent_topic[: -len(_TRI_SUFFIX)]
    finding_text = ""
    conf = 0.5
    reason = ""
    m = _FINDING_RE.search(desc)
    if m:
        try:
            conf = float(m.group(1))
        except Exception:
            conf = 0.5
        finding_text = m.group(2).strip()
        reason = m.group(3).strip()
    return {"parent_topic": parent_topic, "finding": finding_text,
            "confidence": conf, "reason": reason}


async def _find_note_for_topic(parent_topic: str) -> Optional[dict]:
    """Lookup the synthesizer-written finding note for the parent topic.

    synthesizer.py uses `add_note(f"finding/{parent_topic[:60]}", ...)` so we
    match the same prefix and take the most recent.
    """
    prefix = f"finding/{parent_topic[:60]}"
    rows = await kb_sql.find_notes_by_topic(prefix, limit=5)
    return rows[0] if rows else None


async def _list_sources_for_topic(parent_topic: str, limit: int = 8) -> list[dict]:
    """Best-effort: pull sources stored against the parent task by topic match."""
    try:
        async with aiosqlite.connect(config.DB_KB) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT s.url, s.title FROM sources s
                   JOIN tasks t ON s.task_id = t.id
                   WHERE t.topic = ? ORDER BY s.id DESC LIMIT ?""",
                (parent_topic, limit))
            return [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        logger.warning(f"triangulate sources lookup: {e}")
        return []


def _format_sources(sources: list[dict]) -> str:
    if not sources:
        return "  (沒有記錄到具體 source)"
    lines = []
    for s in sources:
        label = s.get("title") or s.get("url") or "(unknown)"
        url = s.get("url")
        lines.append(f"  - {label}" + (f" ({url})" if url and url != label else ""))
    return "\n".join(lines)


def _format_created(ts) -> str:
    if not ts:
        return "(未知)"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return str(ts)


async def ask_about_finding(task: dict) -> tuple[str, dict]:
    """Ask the user about the underlying finding. Returns (outcome, ctx)
    where outcome ∈ {"user_provided", "auto_fallback", "skip", "timeout"}.
    """
    parsed = _parse_triangulate_task(task)
    parent_topic = parsed["parent_topic"]
    note = await _find_note_for_topic(parent_topic)
    finding_text = parsed["finding"] or (note.get("content") if note else "")
    conf = parsed["confidence"]
    if note and note.get("confidence") is not None:
        conf = note["confidence"]
    reason = parsed["reason"] or (note.get("confidence_reason") if note else "")
    sources = await _list_sources_for_topic(parent_topic)

    src_count = note.get("source_count") if note else None
    created = _format_created(note.get("created_at") if note else None)

    q = (
        f"我之前生出了一個 finding:\n\n"
        f"  「{finding_text or '(找不到 finding 內容; task description 缺資料)'}」\n\n"
        f"信心 {conf:.2f}, source_count {src_count if src_count is not None else '?'}\n"
        f"理由: {reason or '(無)'}\n"
        f"建立時間: {created}\n"
        f"所屬 task: {parent_topic}\n\n"
        f"Sources:\n{_format_sources(sources)}\n\n"
        f"我想驗證這個 finding 是否正確 / 完整。你能:\n"
        f"- 提供任何相關資料 / 你個人知道的事實?\n"
        f"- 指正錯誤 / 補充背景?\n"
        f"- 或者直接說「不知道」/「沒意見」, 我會自己去找更多 source。"
    )
    options = [OPTION_PROVIDE, OPTION_AUTO, OPTION_SKIP]
    ctx = {"parsed": parsed, "note": note, "sources": sources}

    try:
        ans = await ask_user(q, options=options, timeout_s=None,
                             asked_by=f"triangulate#{task.get('id')}")
    except Exception as e:
        logger.warning(f"triangulate ask_user failed: {e}")
        ctx["answer"] = None
        return "timeout", ctx

    ctx["answer"] = ans
    a = (ans or "").strip()
    if a == OPTION_SKIP or "跳過" in a or a.lower() in ("skip", "skip ", "ignore"):
        return "skip", ctx
    if a == OPTION_AUTO or "自己研究" in a or "auto" in a.lower():
        return "auto_fallback", ctx
    # OPTION_PROVIDE or any free-text reply → treat as user-provided context.
    return "user_provided", ctx


# ----- Auto-fallback (triangulate_self) ---------------------------------------

_SUMMARY_SYS = (
    "Extract the single core claim of the finding into a short search query "
    "(≤ 12 words, no quotes, no explanation). Output the query verbatim."
)
_ASSESS_SYS = (
    "role: confidence scorer. given a previously low-confidence "
    "finding plus newly fetched independent sources (web + RAG), re-rate "
    "0.0–1.0 (0=pure speculation, 1=multiple independent reliable sources). "
    "Output STRICT JSON: {\"confidence\": float, \"reason\": str}."
)


async def _summarize_claim(finding: str) -> str:
    try:
        resp = await llm.chat(
            [{"role": "system", "content": _SUMMARY_SYS},
             {"role": "user", "content": finding[:1500]}],
            temperature=0.0, max_tokens=4096)
        q = (llm.assistant_text(resp) or "").strip().strip('"').strip("'")
        return (q or finding)[:200]
    except Exception:
        return finding[:120]


async def _safe_web_search(q: str) -> list[dict]:
    try:
        return await asyncio.wait_for(_web_search(q, n=5), timeout=20.0) or []
    except Exception as e:
        logger.warning(f"triangulate_self web_search: {e}")
        return []


async def _safe_rag_search(q: str) -> list[dict]:
    try:
        return await asyncio.wait_for(_rag_search(q, k=5), timeout=10.0) or []
    except Exception as e:
        logger.warning(f"triangulate_self rag_search: {e}")
        return []


async def triangulate_self(task: dict, ctx: dict | None = None) -> dict:
    """Bot-driven fallback: search for ≥2 extra sources, re-score finding."""
    parsed = (ctx or {}).get("parsed") or _parse_triangulate_task(task)
    note = (ctx or {}).get("note")
    if note is None:
        note = await _find_note_for_topic(parsed["parent_topic"])
    finding = parsed.get("finding") or (note.get("content") if note else "")
    if not finding:
        event("triangulate_self_skipped", reason="no_finding_text",
              task_id=task.get("id"))
        return {"status": "no_finding"}

    claim = await _summarize_claim(finding)
    web_results = await _safe_web_search(claim)
    rag_results = await _safe_rag_search(claim)

    body = "Web results:\n"
    for r in (web_results or [])[:5]:
        body += f"- {r.get('title','?')}: {(r.get('body') or '')[:250]}\n"
    body += "\nRAG results:\n"
    for r in (rag_results or [])[:5]:
        body += f"- {(r.get('document') or r.get('text') or str(r))[:250]}\n"

    new_conf = parsed.get("confidence") or 0.5
    new_reason = "auto-triangulation produced no usable evidence"
    try:
        resp = await llm.chat(
            [{"role": "system", "content": _ASSESS_SYS},
             {"role": "user",
              "content": f"Finding:\n{finding}\n\nNew sources:\n{body[:5000]}"}],
            temperature=0.0, max_tokens=4096)
        txt = llm.assistant_text(resp) or ""
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(txt[i:j + 1])
            new_conf = max(0.0, min(1.0, float(obj.get("confidence", new_conf))))
            new_reason = str(obj.get("reason", new_reason))[:300]
    except Exception as e:
        logger.warning(f"triangulate_self assess: {e}")

    if note and note.get("id"):
        try:
            extra = (len(web_results) + len(rag_results))
            await kb_sql.update_note_confidence(
                int(note["id"]), new_conf,
                reason=f"[auto-triangulated] {new_reason}",
                source_count=int(note.get("source_count") or 1) + extra)
        except Exception as e:
            logger.warning(f"triangulate_self update_note: {e}")

    event("triangulate_self_done", task_id=task.get("id"),
          new_conf=new_conf, web_n=len(web_results), rag_n=len(rag_results),
          claim=claim[:80])
    return {"status": "ok", "confidence": new_conf, "reason": new_reason,
            "web_n": len(web_results), "rag_n": len(rag_results),
            "claim": claim}


async def archive_finding(task: dict, ctx: dict | None = None) -> dict:
    """Mark the finding as superseded — user said it's unimportant."""
    parsed = (ctx or {}).get("parsed") or _parse_triangulate_task(task)
    note = (ctx or {}).get("note") or await _find_note_for_topic(parsed["parent_topic"])
    note_id = None
    if note and note.get("id"):
        note_id = int(note["id"])
        try:
            await kb_sql.update_note_confidence(
                note_id, 0.0,
                reason="[archived] user marked unimportant; skipped triangulation",
                source_count=note.get("source_count"))
        except Exception as e:
            logger.warning(f"archive_finding: {e}")
    event("triangulate_skipped", task_id=task.get("id"), note_id=note_id)
    return {"status": "archived", "note_id": note_id}


async def _persist_user_provided(parsed: dict, note: Optional[dict], answer: str) -> None:
    parent_topic = parsed.get("parent_topic", "")
    try:
        await kb_sql.add_note(
            f"finding/{parent_topic[:60]}/user-input", answer[:2000],
            confidence=0.7,
            confidence_reason="user-provided context during triangulation",
            source_count=1)
    except Exception as e:
        logger.warning(f"triangulate persist user-input note: {e}")
    if note and note.get("id"):
        try:
            new_conf = max(0.6, float(note.get("confidence") or 0.0))
            await kb_sql.update_note_confidence(
                int(note["id"]), new_conf,
                reason="user provided supporting context during triangulation",
                source_count=int(note.get("source_count") or 1) + 1)
        except Exception as e:
            logger.warning(f"triangulate bump finding conf: {e}")


async def triangulate(task: dict) -> dict:
    """Top-level entry point used by the research loop for `[triangulate]` tasks.

    Returns {"summary": str} suitable for `complete_task(result=...)`.
    """
    outcome, ctx = await ask_about_finding(task)
    if outcome == "skip":
        await archive_finding(task, ctx)
        return {"summary": "skipped (user marked unimportant)"}
    if outcome == "auto_fallback":
        out = await triangulate_self(task, ctx)
        return {"summary": (
            f"auto-triangulated: conf={out.get('confidence')} "
            f"({out.get('web_n', 0)} web + {out.get('rag_n', 0)} rag); "
            f"reason={out.get('reason','')[:120]}")}
    if outcome == "user_provided":
        ans = (ctx.get("answer") or "").strip()
        if ans == OPTION_PROVIDE:
            return {"summary": "user will provide data via reply (none yet)"}
        await _persist_user_provided(ctx.get("parsed") or {},
                                     ctx.get("note"), ans)
        return {"summary": f"user provided context ({len(ans)} chars)"}
    return {"summary": f"triangulate ended with outcome={outcome}"}
