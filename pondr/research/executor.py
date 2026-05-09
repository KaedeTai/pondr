"""Executor — runs one subtask using the LLM with function calling.

Loops up to N rounds: ask LLM what to do, execute any tool calls, feed
results back. Stops when LLM produces a final text answer or hits round cap.
"""
from __future__ import annotations
import json
from .. import llm
from ..tools import ALL_SCHEMAS, call as call_tool
from ..utils.log import logger, event

SYSTEM = (
    "role: research analyst. use the provided tools to "
    "carry out the subtask. Prefer rag_search before web_search; persist any "
    "useful finding via note_write and rag_store. If you genuinely cannot "
    "decide without a human (ambiguous goal, conflicting evidence, missing "
    "input), call the ask_user tool BEFORE proceeding — but only when truly "
    "necessary. Once done, give a concise final answer (under 200 words). "
    "If you wanted to do something but couldn't because a capability/tool/data/auth was missing, call report_capability_gap(capability, why_needed, severity 1-5, suggested_solution). "
    "Quant tools available: run_backtest(strategy_name, symbol) — strategies "
    "are 'ma_cross', 'mean_reversion', 'breakout'; symbols come from live "
    "ticks (e.g. BTCUSDT, BTC-USD). query_arb_history(symbol, min_spread_bp, "
    "hours) returns cross-exchange opportunities. "
    "query_orderbook_imbalance(symbol, hours, threshold) and "
    "summarize_orderbook(symbol, window_min) read live + persisted bid/ask "
    "imbalance ratios from Binance + Coinbase. If a user asks about an "
    "unsupported strategy or exchange, call report_capability_gap. "
    "curriculum_view() returns a textbook-style outline of what's been "
    "learned so far (chapters → sections → leaves with status + mastery_pct); "
    "use it when the user asks 'what do you know' / 'what have you learned'. "
    "curriculum_deep_dive(node_id) queues a research task on a specific node. "
    "Never invent URLs or numbers."
)

MAX_ROUNDS = 6


async def execute(subtask: dict, parent_topic: str = "") -> dict:
    title = subtask.get("title") or "subtask"
    action = subtask.get("action") or title
    msgs: list[dict] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"Parent topic: {parent_topic}\nSubtask: {title}\nAction: {action}"},
    ]
    if subtask.get("needs_human") and subtask.get("question"):
        # Use ask_user immediately as the first step
        from ..tools.ask import ask_user
        out = await ask_user(question=subtask["question"])
        msgs.append({"role": "user",
                     "content": f"Human answered: {out.get('answer')!r}"})

    transcript: list[dict] = []
    for round_i in range(MAX_ROUNDS):
        resp = await llm.chat(msgs, tools=ALL_SCHEMAS, temperature=0.4, max_tokens=900)
        text = llm.assistant_text(resp)
        calls = llm.assistant_tool_calls(resp)
        if not calls:
            event("subtask_done", title=title, rounds=round_i + 1)
            return {"title": title, "answer": text, "rounds": round_i + 1,
                    "transcript": transcript}
        # append assistant message with tool_calls
        try:
            assistant_msg = (resp.get("choices") or [{}])[0].get("message") or {}
            msgs.append(assistant_msg)
        except Exception:
            msgs.append({"role": "assistant", "content": text or "",
                         "tool_calls": calls})
        for c in calls:
            name = c.get("name")
            try:
                args = json.loads(c.get("arguments") or "{}")
            except Exception:
                args = {}
            result = await call_tool(name, **args)
            transcript.append({"tool": name, "args": args,
                               "result_preview": str(result)[:300]})
            msgs.append({
                "role": "tool",
                "tool_call_id": c.get("id"),
                "name": name,
                "content": json.dumps(result, default=str)[:8000],
            })
    return {"title": title, "answer": "(round cap)", "rounds": MAX_ROUNDS,
            "transcript": transcript}
