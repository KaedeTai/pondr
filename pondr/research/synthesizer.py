"""Synthesizer — combine subtask outputs into a finding + persist + score confidence.

After producing the finding, a second LLM call rates confidence 0-1. Low
confidence (<0.5) auto-enqueues a triangulation task to find independent
sources. Conflicting sources detected during synthesis trigger ask_user.
"""
from __future__ import annotations
import json
from .. import llm
from ..kb import sqlite as kb_sql, chroma
from ..utils.log import event, logger

LOW_CONFIDENCE = 0.5

SYS_SYNTH = (
    "role: synthesizer. given subtask outputs, produce ONE concise "
    "finding (1-3 sentences) and a list of source titles when relevant. "
    "If sources conflict, mention which claims are disputed. "
    "If you wanted to do something but couldn't because a capability/tool/data/"
    "auth was missing, call report_capability_gap(capability, why_needed, "
    "severity 1-5, suggested_solution). "
    "Output JSON: {\"finding\": str, \"sources\": [str], "
    "\"conflicts\": [str], \"source_count\": int}."
)

SYS_SCORE = (
    "role: confidence scorer. given a research finding and the "
    "underlying source material, rate confidence 0.0–1.0 (0 = pure speculation, "
    "1 = multiple independent reliable sources confirming). Penalize: single "
    "source, anonymous source, no source, contradiction. Reward: ≥2 reputable "
    "independent sources, recent date, primary source. "
    "Output STRICT JSON: {\"confidence\": float, \"reason\": str}."
)


async def _score(finding: str, body: str) -> tuple[float, str]:
    resp = await llm.chat([
        {"role": "system", "content": SYS_SCORE},
        {"role": "user",
         "content": f"Finding:\n{finding}\n\nSource material:\n{body[:3000]}"},
    ], temperature=0.0, max_tokens=200)
    txt = llm.assistant_text(resp)
    try:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(txt[i:j + 1])
            c = float(obj.get("confidence", 0.5))
            return max(0.0, min(1.0, c)), str(obj.get("reason", ""))[:300]
    except Exception as e:
        logger.warning(f"score parse: {e}")
    return 0.5, "(scoring failed)"


def _select_important_results(results: list[dict], max_n: int = 5) -> list[dict]:
    """Pick at most max_n sub-task results, prioritising those with the longest
    non-error answers (a rough proxy for 'most informative')."""
    scored: list[tuple[int, dict]] = []
    for r in results or []:
        ans = (r.get("answer") or "")
        # Drop obvious failures
        if not ans or ans.startswith("error:") or ans == "(round cap)":
            score = 0
        else:
            score = len(ans)
        scored.append((score, r))
    # Most informative first; stable for equal scores so original ordering wins
    scored.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in scored[:max_n]]


async def synthesize(parent_topic: str, results: list[dict]) -> dict:
    # Cap to top-5 sub-task results and tighten per-result clip from 1500→800.
    selected = _select_important_results(results, max_n=5)
    body = "\n\n".join(
        f"### {r.get('title')}\n{(r.get('answer') or '')[:800]}" for r in selected)
    resp = await llm.chat([
        {"role": "system", "content": SYS_SYNTH},
        {"role": "user", "content": f"Topic: {parent_topic}\n\n{body}"},
    ], temperature=0.3, max_tokens=700)
    txt = llm.assistant_text(resp) or "(no synthesis)"
    finding = txt[:500]
    conflicts: list[str] = []
    source_count = max(1, len(results))
    try:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(txt[i:j + 1])
            finding = (obj.get("finding") or finding)[:500]
            conflicts = [str(c) for c in (obj.get("conflicts") or [])][:5]
            source_count = max(1, int(obj.get("source_count") or source_count))
    except Exception:
        pass

    confidence, conf_reason = await _score(finding, body)

    note_id = await kb_sql.add_note(
        f"finding/{parent_topic[:60]}", finding,
        confidence=confidence, confidence_reason=conf_reason,
        source_count=source_count)
    await chroma.store(finding, meta={"topic": parent_topic, "kind": "finding",
                                      "confidence": confidence,
                                      "source_count": source_count})
    event("finding", topic=parent_topic, finding=finding[:200],
          confidence=confidence, source_count=source_count,
          conflicts=len(conflicts), note_id=note_id)

    # Low confidence → enqueue triangulation task
    if confidence < LOW_CONFIDENCE:
        try:
            tri_topic = f"[triangulate] {parent_topic} — verify low-confidence finding"
            tri_id = await kb_sql.add_task(
                tri_topic,
                description=(f"Original finding (conf {confidence:.2f}): {finding}\n"
                             f"Reason: {conf_reason}\n"
                             f"Find ≥2 independent sources confirming or refuting."))
            event("triangulate_enqueued", task_id=tri_id, conf=confidence)
        except Exception as e:
            logger.warning(f"triangulate enqueue: {e}")

    # Conflicts → ask user (async, non-blocking)
    if conflicts:
        try:
            from ..server.channels import ask_user
            import asyncio
            async def _ask():
                try:
                    await ask_user(
                        question=(f"Conflicting sources on '{parent_topic[:60]}': "
                                  + " / ".join(conflicts)[:400]
                                  + ". Any preference for which to trust?"),
                        timeout_s=600,
                        asked_by=f"synth-conflict#{note_id}")
                except Exception:
                    pass
            asyncio.create_task(_ask(), name=f"conflict-ask-{note_id}")
        except Exception:
            pass

    return {"finding": finding, "confidence": confidence,
            "confidence_reason": conf_reason, "conflicts": conflicts,
            "source_count": source_count, "note_id": note_id}
