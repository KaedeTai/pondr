"""Planner — decompose a topic into ordered subtasks via LLM."""
from __future__ import annotations
import json
from .. import llm
from ..utils.log import logger, event

SYSTEM = (
    "role: planner. domain: quantitative trading research. "
    "research and market microstructure. Your job NOW is to decompose a research "
    "topic into a short numbered plan of 3-6 concrete subtasks. Each subtask is "
    "a single research action a junior analyst can complete in 5-15 minutes. "
    "When something is genuinely ambiguous, missing input, or requires a human "
    "judgment call, include `ask_user` as a subtask with the question text. "
    "If you wanted to do something but couldn't because a capability/tool/data/auth was missing, call report_capability_gap(capability, why_needed, severity 1-5, suggested_solution). Output STRICT JSON: {\"subtasks\": [{\"title\": str, \"action\": str, "
    "\"needs_human\": bool, \"question\": str|null}]} and nothing else."
)


async def plan(topic: str, context: str = "") -> list[dict]:
    user_msg = f"Research topic:\n{topic}\n\nContext:\n{context or '(none)'}"
    resp = await llm.chat(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": user_msg}],
        temperature=0.3, max_tokens=4096)
    txt = llm.assistant_text(resp)
    subs: list[dict] = []
    if txt:
        # try extract a JSON object
        try:
            i = txt.find("{")
            j = txt.rfind("}")
            if i >= 0 and j > i:
                obj = json.loads(txt[i:j + 1])
                subs = obj.get("subtasks") or []
        except Exception as e:
            logger.warning(f"plan parse failed: {e}; raw: {txt[:200]}")
    if not subs:
        # fallback heuristic decomposition
        subs = [
            {"title": "scan recent literature",
             "action": f"web_search '{topic} quantitative trading 2025'",
             "needs_human": False, "question": None},
            {"title": "tabulate market microstructure facts",
             "action": "summarize recent BTC ticks then store note",
             "needs_human": False, "question": None},
        ]
    event("plan_ready", topic=topic, n=len(subs))
    return subs
