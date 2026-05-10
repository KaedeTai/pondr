"""Reflector — propose follow-up topics from a completed task."""
from __future__ import annotations
import json
from .. import llm
from ..utils.log import logger, event

SYSTEM = (
    "role: reflector. given the original topic and what was found, "
    "propose 0-3 sharp follow-up research topics. If you wanted to do something but couldn't because a capability/tool/data/auth was missing, call report_capability_gap(capability, why_needed, severity 1-5, suggested_solution). Output STRICT JSON: "
    "{\"followups\": [str]}."
)


async def reflect(topic: str, finding: str) -> list[str]:
    resp = await llm.chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"Topic: {topic}\nFinding: {finding}"},
    ], temperature=0.5, max_tokens=4096)
    txt = llm.assistant_text(resp)
    try:
        i = txt.find("{")
        j = txt.rfind("}")
        if i >= 0 and j > i:
            obj = json.loads(txt[i:j + 1])
            followups = [s for s in (obj.get("followups") or []) if isinstance(s, str)][:3]
            event("reflect", topic=topic, n=len(followups))
            return followups
    except Exception as e:
        logger.warning(f"reflect parse: {e}")
    return []
