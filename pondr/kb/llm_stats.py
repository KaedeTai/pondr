"""LLM call stats aggregator.

Reads `data/logs/llm_io.jsonl` (one JSON record per call, written by
`pondr.utils.llm_log`) and computes rolling-window aggregates for the
dashboard's "📊 LLM Stats" card.

The file is grown by every chat() call so we don't keep an in-memory
duplicate of all history — we just stream the file each time the dashboard
asks. For the lifetime/24h/1h windows we sweep once and bucket by ts.
"""
from __future__ import annotations
import asyncio
import json
import time
from datetime import datetime
from typing import Iterable

from .. import config
from ..llm import _LLM_SEMAPHORE, _LLM_CONCURRENCY


# Rough heuristic: latency in the JSONL is wall-clock from chat() entry,
# so it includes both semaphore wait and server time. We use it as-is.

_PERCENTILES = (50, 95)


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").timestamp()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp()
        except Exception:
            return None


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _prompt_preview(rec: dict, n: int = 50) -> str:
    msgs = rec.get("messages")
    if isinstance(msgs, list) and msgs:
        # messages may have been str-coerced by _sanitize; handle both shapes
        first = msgs[-1] if isinstance(msgs, list) else msgs
        content = ""
        if isinstance(first, dict):
            content = first.get("content") or ""
        else:
            content = str(first)
        return content.replace("\n", " ")[:n]
    if isinstance(msgs, str):
        return msgs.replace("\n", " ")[:n]
    return ""


def _iter_recent(window_s: float) -> Iterable[dict]:
    """Yield records from the JSONL whose ts is within window_s of now."""
    cutoff = time.time() - window_s
    path = config.LLM_LOG_PATH
    try:
        # Read tail-only; we don't need the entire 1k+ line history every call.
        # File is tiny (~10MB max) so a full scan is fine, but we cap reads at
        # a sensible upper bound on lines.
        with path.open("r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    # Walk from newest end; stop early once we cross the cutoff
    for line in reversed(lines[-5000:]):
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts = _parse_ts(r.get("ts"))
        if ts is None:
            continue
        if ts < cutoff:
            break
        r["_ts"] = ts
        out.append(r)
    return out


def _aggregate(window_s: float) -> dict:
    recs = list(_iter_recent(window_s))
    n = len(recs)
    latencies: list[float] = []
    prompts: list[int] = []
    completions: list[int] = []
    by_kind: dict[str, int] = {}
    failures = 0
    tok_per_s_vals: list[float] = []
    slowest: list[dict] = []

    # Bucket by minute for sparklines (last 60 minutes only — defensive cap)
    minute_bucket_count: dict[int, int] = {}
    minute_bucket_lat_sum: dict[int, float] = {}
    minute_bucket_lat_n: dict[int, int] = {}

    now_min = int(time.time() // 60)
    for r in recs:
        kind = r.get("kind") or "?"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if "error" in kind:
            failures += 1
        latency = r.get("latency_ms") or 0
        try:
            latency = int(latency)
        except Exception:
            latency = 0
        latencies.append(float(latency))
        toks = r.get("tokens") or {}
        try:
            p = int(toks.get("prompt") or 0)
            c = int(toks.get("completion") or 0)
        except Exception:
            p, c = 0, 0
        prompts.append(p)
        completions.append(c)
        if latency > 0 and c > 0:
            tok_per_s_vals.append(c * 1000.0 / latency)
        slowest.append({
            "ts": r.get("ts"),
            "kind": kind,
            "latency_ms": latency,
            "prompt_preview": _prompt_preview(r, 50),
            "prompt_tokens": p,
            "completion_tokens": c,
        })
        # Sparkline buckets — only for the past 60 minutes window
        ts_min = int(r["_ts"] // 60)
        if now_min - ts_min < 60:
            minute_bucket_count[ts_min] = minute_bucket_count.get(ts_min, 0) + 1
            minute_bucket_lat_sum[ts_min] = minute_bucket_lat_sum.get(ts_min, 0.0) + float(latency)
            minute_bucket_lat_n[ts_min] = minute_bucket_lat_n.get(ts_min, 0) + 1

    slowest.sort(key=lambda d: d["latency_ms"], reverse=True)
    slowest = slowest[:5]

    def _safe_mean(xs: list) -> float:
        return float(sum(xs) / len(xs)) if xs else 0.0

    out: dict = {
        "window_s": window_s,
        "count": n,
        "mean_latency_ms": round(_safe_mean(latencies), 1),
        "max_latency_ms": int(max(latencies)) if latencies else 0,
        "mean_prompt_tokens": round(_safe_mean(prompts), 1),
        "mean_completion_tokens": round(_safe_mean(completions), 1),
        "total_prompt_tokens": int(sum(prompts)),
        "total_completion_tokens": int(sum(completions)),
        "mean_compl_tok_per_s": round(_safe_mean(tok_per_s_vals), 2),
        "by_kind": by_kind,
        "failures": failures,
        "top_slowest": slowest,
    }
    for pct in _PERCENTILES:
        out[f"p{pct}_latency_ms"] = round(_percentile(latencies, pct), 1)

    # 60-minute sparkline arrays (oldest → newest)
    calls_per_min: list[int] = []
    mean_lat_per_min: list[float] = []
    for off in range(59, -1, -1):
        mb = now_min - off
        calls_per_min.append(minute_bucket_count.get(mb, 0))
        if minute_bucket_lat_n.get(mb):
            mean_lat_per_min.append(round(
                minute_bucket_lat_sum[mb] / minute_bucket_lat_n[mb], 1))
        else:
            mean_lat_per_min.append(0.0)
    out["calls_per_min_60"] = calls_per_min
    out["mean_latency_per_min_60"] = mean_lat_per_min
    return out


async def get_stats(window_hours: int = 24) -> dict:
    """Public entry: return a JSON-serializable stats dict for the given window.

    Reading the JSONL file is offloaded to a thread to keep the event loop
    responsive (file may grow into the megabyte range).
    """
    window_s = float(window_hours) * 3600.0
    main = await asyncio.to_thread(_aggregate, window_s)
    # Include 1h count alongside (cheap second pass, same scan domain bounded by 1h)
    if window_hours >= 1:
        h1 = await asyncio.to_thread(_aggregate, 3600.0)
        main["count_1h"] = h1["count"]
    else:
        main["count_1h"] = main["count"]
    main["count_lifetime"] = await asyncio.to_thread(_lifetime_count)
    main["window_hours"] = window_hours
    # Live: how many slots the LLM semaphore is currently letting through
    main["llm_concurrency_limit"] = _LLM_CONCURRENCY
    try:
        # _value is the number of currently-available permits; missing permits
        # = active in-flight calls.
        free = getattr(_LLM_SEMAPHORE, "_value", _LLM_CONCURRENCY)
        main["llm_in_flight"] = max(0, _LLM_CONCURRENCY - int(free))
    except Exception:
        main["llm_in_flight"] = 0
    return main


def _lifetime_count() -> int:
    path = config.LLM_LOG_PATH
    try:
        with path.open("rb") as f:
            n = 0
            for _ in f:
                n += 1
            return n
    except FileNotFoundError:
        return 0
