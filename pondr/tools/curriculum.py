"""LLM tools for the curriculum view."""
from __future__ import annotations

from ..kb import curriculum as curr_kb, sqlite as kb_sql
from ..utils.log import event


async def curriculum_view(max_depth: int = 4) -> dict:
    """Return a compact summary of the current curriculum tree.

    The full tree (potentially with hundreds of note ids) is too verbose for
    normal LLM context, so this returns just titles, statuses, and mastery
    aggregates.
    """
    tree = await curr_kb.tree()
    overall = await curr_kb.overall_mastery()
    counts = await curr_kb.counts_by_status()

    def _shorten(node: dict, depth: int = 0) -> dict:
        out = {
            "id": node["id"],
            "title": node["title"],
            "status": node.get("status"),
            "mastery_pct": round(node.get("mastery_pct") or 0, 1),
            "n_notes": len(node.get("note_ids") or []),
            "n_backtests": len(node.get("related_backtest_ids") or []),
            "n_gaps": len(node.get("related_gap_ids") or []),
        }
        if depth + 1 < max_depth:
            children = node.get("children") or []
            if children:
                out["children"] = [_shorten(c, depth + 1) for c in children]
        return out

    return {
        "overall_mastery_pct": round(overall, 1),
        "counts": counts,
        "tree": [_shorten(r, 0) for r in tree],
    }


VIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "curriculum_view",
        "description": (
            "Return a compact textbook-style outline of what pondr has "
            "learned so far. Each node carries a status (solid/medium/open) "
            "and mastery_pct, plus counts of supporting notes / backtests / "
            "open gaps. Use this when the user asks 'what do you know' / "
            "'你目前學會什麼' / 'what's your current understanding of X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_depth": {"type": "integer", "default": 4,
                              "description": "Limit tree depth"},
            },
        },
    },
}


async def curriculum_deep_dive(node_id: int) -> dict:
    """Enqueue a research subtask asking the bot to deepen a specific node."""
    title = await curr_kb.deep_dive_task_title(int(node_id))
    if not title:
        return {"ok": False, "error": f"node {node_id} not found"}
    tid = await kb_sql.add_task(
        title,
        description=f"Curriculum drill-down requested for node id={node_id}")
    event("curriculum_deep_dive", node_id=node_id, task_id=tid, title=title)
    return {"ok": True, "task_id": tid, "title": title}


DEEP_DIVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "curriculum_deep_dive",
        "description": (
            "Queue a research task to dig deeper on a specific curriculum "
            "node. Use when the user asks to learn more about a topic that's "
            "currently 'open' or 'medium' status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "integer"},
            },
            "required": ["node_id"],
        },
    },
}
