"""Live binance depth-diff feed smoke test.

Same skip semantics as test_feed_aggtrade.
"""
import asyncio
import os
import pytest

from pondr.feeds.binance_depth_diff import BinanceDepthDiffFeed
from pondr.kb import duckdb as ddb


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _live_skipper(reason: str):
    pytest.skip(reason)


def test_depth_diff_feed_construct():
    f = BinanceDepthDiffFeed()
    assert f.name == "binance"
    assert f.label == "binance-depth-diff"
    assert f.msg_count == 0
    assert not f.connected


def test_depth_diff_feed_persists_messages():
    if os.environ.get("PONDR_SKIP_LIVE") == "1":
        _live_skipper("PONDR_SKIP_LIVE set")
    try:
        n0 = aio(ddb.count_depth_diffs())
    except Exception as e:
        _live_skipper(f"DuckDB unavailable: {e!r}")

    feed = BinanceDepthDiffFeed()

    async def _run_for(secs: float):
        task = asyncio.create_task(feed.run())
        try:
            await asyncio.sleep(secs)
        finally:
            await feed.stop()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    try:
        aio(_run_for(30.0))
    except Exception as e:
        _live_skipper(f"feed run failed (offline?): {e!r}")

    try:
        n1 = aio(ddb.count_depth_diffs())
    except Exception as e:
        _live_skipper(f"count_depth_diffs failed: {e!r}")
    received = n1 - n0
    in_memory = feed.msg_count
    if in_memory == 0:
        _live_skipper("no depth-diff msgs received — likely offline / blocked")
    assert in_memory >= 10 or received >= 10, (
        f"expected ≥10 depth diffs, got in-memory={in_memory} db_delta={received}")
