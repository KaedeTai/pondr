"""Chat router 'feature_request' action writes to capability_gaps KB."""
import asyncio
import uuid

from pondr import __main__ as pondr_main
from pondr.kb import capability_gaps as cap_kb


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_feature_request_creates_capability_gap():
    cap_name = f"feed:test-{uuid.uuid4().hex[:8]}"
    obj = {
        "action": "feature_request",
        "capability": cap_name,
        "why_needed": "user wants to subscribe to kraken WS",
        "severity": 3,
        "suggested_solution": "use websockets to wss://ws.kraken.com",
    }
    out = aio(pondr_main._handle_feature_request(obj, "應該也訂閱 kraken WS"))
    assert out["type"] == "feature_acknowledged"
    assert out["capability"] == cap_name
    assert out["gap_id"]
    assert out["severity"] == 3
    assert out["created"] is True
    # Verify the gap is actually in the DB
    row = aio(cap_kb.get(out["gap_id"]))
    assert row is not None
    assert row["capability"] == cap_name
    assert row["severity"] == 3
    assert "kraken" in (row["suggested_solution"] or "").lower()


def test_feature_request_clamps_severity():
    cap_name = f"feed:clamp-{uuid.uuid4().hex[:8]}"
    obj = {
        "action": "feature_request",
        "capability": cap_name,
        "why_needed": "x",
        "severity": 99,  # out-of-range
    }
    out = aio(pondr_main._handle_feature_request(obj, "msg"))
    assert out["severity"] == 5
    row = aio(cap_kb.get(out["gap_id"]))
    assert row["severity"] == 5


def test_feature_request_defaults_when_severity_missing():
    cap_name = f"feed:default-{uuid.uuid4().hex[:8]}"
    obj = {
        "action": "feature_request",
        "capability": cap_name,
        "why_needed": "x",
    }
    out = aio(pondr_main._handle_feature_request(obj, "msg"))
    assert out["severity"] == 3


def test_feature_request_handles_nonint_severity():
    cap_name = f"feed:nonint-{uuid.uuid4().hex[:8]}"
    obj = {
        "action": "feature_request",
        "capability": cap_name,
        "why_needed": "x",
        "severity": "high-please",  # garbage
    }
    out = aio(pondr_main._handle_feature_request(obj, "msg"))
    assert out["severity"] == 3


def test_feature_request_default_reply_includes_gap_id():
    cap_name = f"feed:reply-{uuid.uuid4().hex[:8]}"
    obj = {
        "action": "feature_request",
        "capability": cap_name,
        "why_needed": "x",
    }
    out = aio(pondr_main._handle_feature_request(obj, "msg"))
    assert f"#{out['gap_id']}" in out["msg"]
    assert cap_name in out["msg"]
