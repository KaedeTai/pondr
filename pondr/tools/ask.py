"""ask_user — bot-initiated question to the human via channel mux.

Question is persisted to SQLite (pondr.kb.questions) so it survives bot
restarts and can be replayed to any (re)connecting channel.

If the user has a `language` preference (e.g. 繁體中文) and the question or
options look like a different language, we run a quick translation LLM call
before broadcasting — a backstop for when the upstream LLM ignored the prefs
directive in its system prompt.
"""
from __future__ import annotations
import re

from ..server.channels import ask_user as _ask
from ..utils.log import logger


def _current_task_label() -> str | None:
    try:
        from ..research import CURRENT
        if CURRENT.get("task_id"):
            return f"task#{CURRENT['task_id']} ({(CURRENT.get('topic') or '')[:60]})"
    except Exception:
        pass
    return None


# A naïve "is this Chinese / Japanese already?" check — if so, don't bother
# asking the LLM to translate when target is 繁體中文.
_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")


def _looks_like(lang: str, text: str) -> bool:
    """Best-effort same-language detector. Conservative: only suppresses the
    translate call when we're confident the text is already in target lang.
    """
    if not text:
        return True
    cjk_count = sum(1 for c in text if _CJK_RE.search(c))
    cjk_ratio = cjk_count / max(1, len(text))
    target = (lang or "").lower()
    if any(k in target for k in ("中文", "chinese", "中文", "繁體", "繁中",
                                 "簡中", "zh", "zh-tw", "zh-cn")):
        return cjk_ratio >= 0.2
    if any(k in target for k in ("日本", "japanese", "jp", "ja")):
        return cjk_ratio >= 0.2
    if any(k in target for k in ("english", "en", "英文", "英語")):
        return cjk_ratio < 0.05
    # Unknown language — don't claim it's already correct
    return False


async def _translate_if_needed(question: str,
                               options: list[str] | None) -> tuple[str, list[str] | None]:
    """If a `language` preference is active and the input looks like another
    language, run one LLM call to translate. Best-effort — failures pass
    through verbatim so we never block the question."""
    try:
        from ..kb import preferences as prefs_kb
        rows = await prefs_kb.list_active()
        lang = prefs_kb.get_language(rows) if hasattr(prefs_kb, "get_language") else None
    except Exception:
        return question, options
    if not lang:
        return question, options
    needs_q = not _looks_like(lang, question)
    needs_opts = bool(options) and any(not _looks_like(lang, o) for o in options)
    if not (needs_q or needs_opts):
        return question, options
    try:
        from .. import llm
        # Stuff everything in one call. Returns JSON for unambiguous mapping.
        import json
        payload = {"question": question, "options": options or []}
        prompt = (
            f"Translate the JSON below into {lang}. Preserve the JSON "
            f"structure exactly. Only translate the human-readable strings; "
            f"do NOT translate identifiers (symbol names like BTCUSDT, URLs, "
            f"exchange IDs, strategy names like 'ma_cross', or numeric IDs). "
            f"Return ONLY valid JSON, no surrounding prose.\n\n"
            f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        # User-facing translation — pass the explicit hint. (Global pref
        # injection is gone; only call sites that want the directive opt in.)
        resp = await llm.chat(
            [{"role": "system",
              "content": "You are a precise translator. Output JSON only."},
             {"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=600, language_hint=lang)
        txt = llm.assistant_text(resp).strip()
        # Allow ```json fences
        if txt.startswith("```"):
            txt = txt.strip("`")
            if txt.lower().startswith("json"):
                txt = txt[4:]
            txt = txt.strip()
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            return question, options
        obj = json.loads(txt[i:j + 1])
        new_q = (obj.get("question") or question).strip() or question
        new_opts = obj.get("options")
        if not isinstance(new_opts, list) or len(new_opts) != len(options or []):
            new_opts = options
        else:
            new_opts = [str(o) for o in new_opts]
        if new_q != question or new_opts != options:
            logger.info(f"ask_user: translated to {lang}: "
                        f"{question[:60]!r} → {new_q[:60]!r}")
        return new_q, new_opts
    except Exception as e:
        logger.warning(f"ask_user translate fallback failed: {e}")
        return question, options


async def ask_user(question: str, options: list[str] | None = None,
                   timeout_s: int | None = None) -> dict:
    asked_by = _current_task_label()
    question, options = await _translate_if_needed(question, options)
    try:
        ans = await _ask(question, options=options, timeout_s=timeout_s,
                         asked_by=asked_by)
        return {"answer": ans, "timed_out": False}
    except Exception as e:
        logger.warning(f"ask_user failed/timeout: {e}")
        return {"answer": None, "timed_out": True, "error": repr(e)}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the human user a question via all active channels and await "
            "their reply. The question is persisted across restarts. Use ONLY "
            "when you genuinely need a human decision: ambiguous goal, "
            "conflicting evidence, missing critical input, or before any "
            "irreversible action. Returns {answer, timed_out}. If timeout_s "
            "is set, returns timed_out=true after waiting. "
            "Write the question in the user's language preference if set."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "timeout_s": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
}
