"""Capability gap detection tests."""
import asyncio
import uuid
from pondr.kb import capability_gaps as cap_kb


def aio(c): return asyncio.get_event_loop().run_until_complete(c)
def cap(p): return f"test:{p}-{uuid.uuid4().hex[:8]}"


def test_first_report_creates():
    name = cap("create")
    out = aio(cap_kb.report(name, "needed for K-line analysis", 3,
                             "use playwright + tradingview"))
    assert out["ok"] and out["created"]
    assert out["row"]["report_count"] == 1


def test_repeat_report_debounces():
    name = cap("dup")
    aio(cap_kb.report(name, "first reason", 2))
    aio(cap_kb.report(name, "second reason", 3))
    rows = [r for r in aio(cap_kb.list_open()) if r["capability"] == name]
    assert rows and rows[0]["report_count"] == 2
    assert rows[0]["severity"] == 3


def test_high_severity_flag():
    name = cap("high")
    out = aio(cap_kb.report(name, "blocking critical task", 5))
    assert out["is_high"]


def test_status_transition():
    name = cap("dismissable")
    out = aio(cap_kb.report(name, "minor", 1))
    cap_id = out["row"]["id"]
    assert aio(cap_kb.update_status(cap_id, "dismissed", notes="not needed"))
    assert aio(cap_kb.get(cap_id))["status"] == "dismissed"


def test_list_open_excludes_resolved():
    name = cap("resolved")
    out = aio(cap_kb.report(name, "x", 2))
    aio(cap_kb.update_status(out["row"]["id"], "resolved"))
    assert not [r for r in aio(cap_kb.list_open()) if r["capability"] == name]


def test_tool_schema():
    from pondr.tools import capability
    assert capability.SCHEMA["function"]["name"] == "report_capability_gap"
    assert "severity" in capability.SCHEMA["function"]["parameters"]["properties"]
