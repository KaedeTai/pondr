"""Dashboard import + state shape smoke."""
import asyncio
from pondr.server import dashboard


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def test_state_shape():
    import pytest, _duckdb
    try:
        s = aio(dashboard._state())
    except _duckdb.IOException:
        pytest.skip("market_ticks.db is locked (bot running)")
    for k in ("uptime_s", "kb_counts", "ticks_total", "channels",
              "tasks_queued", "events", "llm_recent", "questions"):
        assert k in s, f"missing {k}"


def test_app_routes():
    routes = {r.path for r in dashboard.app.routes}
    assert "/" in routes and "/api/state" in routes and "/ws/state" in routes
