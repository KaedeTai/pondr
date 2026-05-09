"""report_capability_gap — bot says "I need capability X but lack it"."""
from __future__ import annotations
from ..kb import capability_gaps as cap_kb
from ..server.channels import MUX, ask_user as channel_ask
from ..utils.log import logger, event


async def report_capability_gap(capability: str, why_needed: str = "",
                                severity: int = 3,
                                suggested_solution: str | None = None) -> dict:
    out = await cap_kb.report(capability, why_needed, severity, suggested_solution)
    if not out.get("ok"):
        return out
    row = out["row"]
    event("capability_gap_reported",
          capability=capability, severity=severity,
          report_count=row["report_count"], created=out["created"])
    # Broadcast to channels — debounced to avoid spam (only on first report or
    # when severity bumps to 4+)
    try:
        if out["created"] or out["is_high"]:
            await MUX.send({
                "type": "capability_gap",
                "id": row["id"],
                "capability": capability,
                "severity": severity,
                "why_needed": why_needed,
                "suggested_solution": suggested_solution,
                "report_count": row["report_count"],
            })
    except Exception as e:
        logger.warning(f"capability_gap broadcast: {e}")
    # High severity AND first report → ask user immediately (best-effort, short timeout)
    if out["created"] and out["is_high"]:
        try:
            q = (f"⚠️ Bot needs capability it lacks: {capability!r} "
                 f"(severity {severity}/5). Why: {why_needed[:200]}. "
                 f"Suggested: {suggested_solution or '(none)'}. "
                 f"Want to help install / configure?")
            ans = await channel_ask(q, options=["yes, help me install",
                                                "no, work without it"],
                                    timeout_s=300,
                                    asked_by=f"capability_gap#{row['id']}")
            return {"ok": True, "row": row, "user_response": ans}
        except Exception as e:
            return {"ok": True, "row": row, "user_response_error": repr(e)}
    return {"ok": True, "row": row}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "report_capability_gap",
        "description": (
            "Tell the user: 'I need a capability I don't have'. Use whenever "
            "you wanted to do something but lack the tool/data/auth/library "
            "(e.g. 'fetch real-time TradingView chart', 'access user's "
            "brokerage', 'read PDF X page Y'). Repeated reports of the same "
            "capability are debounced server-side. Severity 1-5 (5 = blocking)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string",
                               "description": "Concise name of the missing capability"},
                "why_needed": {"type": "string",
                               "description": "Current task context that triggered this"},
                "severity": {"type": "integer", "minimum": 1, "maximum": 5,
                             "default": 3},
                "suggested_solution": {"type": "string",
                                       "description": "What might fix this (e.g. 'pip install X')"},
            },
            "required": ["capability"],
        },
    },
}
