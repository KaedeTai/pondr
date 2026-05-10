"""LLM client wrapper around openai-python pointing at local OpenAI-compat server.

All calls are logged to JSONL via utils.llm_log. Includes graceful retry that
returns a structured stub instead of raising — keeps the research loop alive.
"""
from __future__ import annotations
import asyncio
import json
import os
import time
import uuid
from typing import Any
from openai import AsyncOpenAI, APIError

from . import config
from .utils.log import logger, event
from .utils import llm_log

_client: AsyncOpenAI | None = None

# Serialize concurrent LLM calls so we don't pile multiple requests on a
# llama-server slot configured with `--parallel 1`. Override via
# PONDR_LLM_CONCURRENCY env (raise to N if the server is reconfigured for
# `--parallel N`). Default 1 = strict serialization.
_LLM_CONCURRENCY = max(1, int(os.getenv("PONDR_LLM_CONCURRENCY", "1")))
_LLM_SEMAPHORE = asyncio.Semaphore(_LLM_CONCURRENCY)


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
    return _client


def _extract_function_calls(resp_dict: dict) -> list[dict]:
    out = []
    try:
        for ch in resp_dict.get("choices", []):
            msg = ch.get("message", {}) or {}
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                out.append({
                    "id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                })
    except Exception:
        pass
    return out




def _prepend_language_hint(messages: list[dict], lang: str | None) -> list[dict]:
    """Prepend a tiny output-language directive when caller passed one.

    `輸出繁體中文` is ~6 tokens vs the old MUST-FOLLOW block's ~300. Only
    user-facing call sites (chat answers, dashboard-visible curriculum,
    ask_user translation) pass a hint; internal calls (router, planner,
    executor, synthesizer, reflector) leave it None and pay zero overhead.
    """
    if not lang:
        return messages
    return [{"role": "system", "content": f"輸出{lang}"}, *messages]


async def chat(messages: list[dict], *, tools: list[dict] | None = None,
               temperature: float = 0.4, max_tokens: int = 4096,
               attempts: int = 3, trace_id: str | None = None,
               language_hint: str | None = None) -> dict:
    """OpenAI chat.completions wrapper. Returns dict-like response. Never raises;
    on failure returns a stub dict with error field set.

    Pass `language_hint` only on calls whose output is shown to the user.
    """
    trace_id = trace_id or str(uuid.uuid4())
    messages = _prepend_language_hint(messages, language_hint)
    last_err: Exception | None = None
    for i in range(attempts):
        t0 = time.monotonic()
        try:
            kwargs: dict[str, Any] = dict(model=config.LLM_MODEL, messages=messages,
                                          temperature=temperature, max_tokens=max_tokens)
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            # Serialize against the llama-server slot. The await timer measures
            # both queue + server time so caller-side latency stays meaningful.
            async with _LLM_SEMAPHORE:
                resp = await asyncio.wait_for(
                    client().chat.completions.create(**kwargs), timeout=240)
            latency = int((time.monotonic() - t0) * 1000)
            try:
                rd = resp.model_dump()
            except Exception:
                rd = json.loads(resp.json()) if hasattr(resp, "json") else {}
            usage = rd.get("usage") or {}
            llm_log.log_call(
                "chat_completion", config.LLM_MODEL, messages, tools, rd,
                _extract_function_calls(rd), latency,
                {"prompt": usage.get("prompt_tokens", 0),
                 "completion": usage.get("completion_tokens", 0)},
                trace_id=trace_id,
            )
            return rd
        except (APIError, asyncio.TimeoutError, Exception) as e:
            last_err = e
            latency = int((time.monotonic() - t0) * 1000)
            logger.warning(f"LLM call failed ({i+1}/{attempts}): {e!r}")
            llm_log.log_call(
                "chat_completion_error", config.LLM_MODEL, messages, tools,
                {"error": repr(e)}, [], latency, {}, trace_id=trace_id,
            )
            await asyncio.sleep(2 ** i)
    event("llm_failed", error=repr(last_err))
    # Return a synthetic empty answer so caller can keep going
    return {
        "error": repr(last_err),
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "error"}],
    }


def assistant_text(resp: dict) -> str:
    try:
        return (resp["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""


def assistant_tool_calls(resp: dict) -> list[dict]:
    return _extract_function_calls(resp)
