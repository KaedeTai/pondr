"""Channels + research loop scaffolding smoke."""
import asyncio
from pondr.server.channels import build_channels, MUX
from pondr.server.channels.base import _PENDING


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def test_channels_build_default():
    mux = build_channels(["stdio"])  # ws skip in test (port may be busy)
    assert any(c.name == "stdio" for c in mux.channels)


def test_planner_fallback():
    from pondr.research.planner import plan
    # Force LLM error path: hit unreachable URL by patching base
    import pondr.config as cfg
    saved = cfg.LLM_BASE_URL
    cfg.LLM_BASE_URL = "http://127.0.0.1:1/v1"  # unreachable
    import pondr.llm as L
    L._client = None
    subs = aio(plan("test topic"))
    cfg.LLM_BASE_URL = saved
    L._client = None
    assert isinstance(subs, list) and subs
