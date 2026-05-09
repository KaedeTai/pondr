#!/usr/bin/env python3
"""Backfill: translate any pending pending_questions into the user's
preferred language in one batched LLM call.

Usage:
    python scripts/translate_pending_questions.py [--dry-run]

Idempotent — re-running on already-translated questions is cheap (the LLM
just returns them unchanged) and the heuristic in `ask_user._looks_like`
shortcuts them. The script DOES NOT broadcast a `question_updated` event
itself, but if the bot is running the next /api/state poll picks the new
text up automatically (the dashboard reads from SQLite each refresh).

Safe to run while pondr is running.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make the repo importable when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from pondr import config, llm
from pondr.kb import preferences as prefs_kb
from pondr.kb import questions as q_kb
from pondr.tools.ask import _looks_like


SYS = ("You are a precise translator. Translate every item in the JSON list "
       "into the requested language. Preserve identifiers (URLs, symbol "
       "names like BTCUSDT, exchange IDs, strategy names like 'ma_cross'). "
       "Output ONLY a valid JSON list of strings, in the same order, same "
       "length. No surrounding prose.")


async def _translate_batch(items: list[str], lang: str) -> list[str]:
    if not items:
        return []
    prompt = (f"Translate every string in this JSON list into {lang}. "
              f"Return a JSON list of the same length.\n\n"
              f"INPUT:\n{json.dumps(items, ensure_ascii=False)}")
    resp = await llm.chat(
        [{"role": "system", "content": SYS},
         {"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=2000)
    txt = llm.assistant_text(resp).strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        if txt.lower().startswith("json"):
            txt = txt[4:]
        txt = txt.strip()
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j <= i:
        raise RuntimeError(f"Translator returned non-JSON: {txt[:200]!r}")
    out = json.loads(txt[i:j + 1])
    if not isinstance(out, list) or len(out) != len(items):
        raise RuntimeError(
            f"Translator returned {len(out) if isinstance(out, list) else '?'} "
            f"items, expected {len(items)}")
    return [str(x) for x in out]


async def main(dry_run: bool = False):
    await q_kb.init()
    await prefs_kb.init()
    rows = await prefs_kb.list_active()
    lang = prefs_kb.get_language(rows)
    if not lang:
        print("No `language` preference set — nothing to do.")
        return 0
    print(f"Target language: {lang!r}")

    pending = await q_kb.list_pending()
    print(f"{len(pending)} pending questions on disk")
    todo = [q for q in pending
            if q.get("question") and not _looks_like(lang, q["question"])]
    print(f"  → {len(todo)} appear to need translation")

    if not todo:
        return 0

    questions = [q["question"] for q in todo]
    print("\nBefore:")
    for q in todo:
        print(f"  [{q['qid'][:8]}] {q['question'][:120]}")

    translated = await _translate_batch(questions, lang)

    print("\nAfter:")
    for q, t in zip(todo, translated):
        print(f"  [{q['qid'][:8]}] {t[:120]}")

    if dry_run:
        print("\n--dry-run: not writing.")
        return 0

    async with aiosqlite.connect(config.DB_KB) as db:
        for q, new_text in zip(todo, translated):
            await db.execute(
                "UPDATE pending_questions SET question=? "
                "WHERE qid=? AND status='pending'",
                (new_text, q["qid"]))
        await db.commit()
    print(f"\nUpdated {len(todo)} rows. The running pondr will pick up the "
          f"new text on the next dashboard poll / channel replay.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would change without writing.")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run)))
