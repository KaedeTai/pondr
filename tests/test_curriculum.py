"""Curriculum kb + generator validation/diff helpers."""
from __future__ import annotations
import asyncio
import pytest

from pondr import config
from pondr.kb import curriculum as curr_kb


def aio(c):
    return asyncio.get_event_loop().run_until_complete(c)


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Each test gets its own fresh SQLite KB so we don't pollute the real one."""
    monkeypatch.setattr(config, "DB_KB", tmp_path / "kb.db")
    aio(curr_kb.init())
    yield


def _spec_two_chapters():
    return [
        {"title": "經典策略",
         "status": "medium",
         "mastery_pct": 65.0,
         "description": "傳統 alpha-seeking 策略",
         "children": [
             {"title": "1.1 Momentum",
              "status": "solid",
              "mastery_pct": 85.0,
              "note_ids": [1, 2, 3],
              "related_backtest_ids": [10]},
             {"title": "1.2 Mean reversion",
              "status": "medium",
              "mastery_pct": 55.0,
              "note_ids": [4]},
         ]},
        {"title": "風險管理",
         "status": "open",
         "mastery_pct": 30.0,
         "children": [
             {"title": "2.1 Kelly",
              "status": "open",
              "mastery_pct": 30.0},
         ]},
    ]


def test_replace_tree_round_trip():
    out = aio(curr_kb.replace_tree(_spec_two_chapters()))
    assert out["inserted"] == 5  # 2 chapters + 3 leaves
    rows = aio(curr_kb.list_all())
    titles = {r["title"] for r in rows}
    assert "經典策略" in titles
    assert "1.1 Momentum" in titles
    # parent linkage preserved
    parents = {r["title"]: r["parent_id"] for r in rows}
    chap_id = next(r["id"] for r in rows if r["title"] == "經典策略")
    assert parents["1.1 Momentum"] == chap_id


def test_tree_nesting():
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    tree = aio(curr_kb.tree())
    assert len(tree) == 2
    assert tree[0]["title"] == "經典策略"
    assert len(tree[0]["children"]) == 2
    assert tree[0]["children"][0]["title"] == "1.1 Momentum"


def test_get_node():
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    rows = aio(curr_kb.list_all())
    leaf = next(r for r in rows if r["title"] == "1.1 Momentum")
    n = aio(curr_kb.get_node(leaf["id"]))
    assert n is not None
    assert n["status"] == "solid"
    assert n["note_ids"] == [1, 2, 3]
    assert n["related_backtest_ids"] == [10]


def test_replace_is_atomic_and_clears():
    """Calling replace_tree a second time must wipe the previous tree."""
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    aio(curr_kb.replace_tree([
        {"title": "新章節", "status": "open", "mastery_pct": 0.0}]))
    rows = aio(curr_kb.list_all())
    assert len(rows) == 1
    assert rows[0]["title"] == "新章節"


def test_overall_mastery_average_of_roots():
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    overall = aio(curr_kb.overall_mastery())
    assert abs(overall - (65 + 30) / 2) < 1e-6


def test_counts_by_status():
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    c = aio(curr_kb.counts_by_status())
    assert c["solid"] == 1
    assert c["medium"] == 2  # one chapter + one leaf
    assert c["open"] == 2     # one chapter + one leaf


def test_validate_tree_drops_invalid_ids():
    """Generator validation should strip note_ids that don't exist."""
    from pondr.research.curriculum import _validate_tree
    notes = [{"id": 1, "confidence": 0.8}, {"id": 2, "confidence": 0.5}]
    raw = [{"title": "X", "status": "solid",
            "note_ids": [1, 99, "not_a_number"],
            "related_backtest_ids": [],
            "related_gap_ids": [],
            "children": []}]
    cleaned = _validate_tree(raw, notes, [], [])
    assert cleaned[0]["note_ids"] == [1]


def test_validate_tree_drops_titleless_node():
    from pondr.research.curriculum import _validate_tree
    raw = [{"title": "", "children": [{"title": "good"}]},
           {"title": "ok"}]
    cleaned = _validate_tree(raw, [], [], [])
    titles = [n["title"] for n in cleaned]
    assert "ok" in titles and "" not in titles


def test_diff_summary_promotion():
    from pondr.research.curriculum import _diff_summary
    prev = [{"title": "A", "status": "open"},
            {"title": "B", "status": "medium"}]
    new = [{"title": "A", "status": "medium"},
           {"title": "B", "status": "medium"},
           {"title": "C", "status": "open"}]
    d = _diff_summary(prev, new)
    assert d["promoted"] == ["A: open→medium"]
    assert d["new"] == ["C"]
    assert d["demoted"] == []


def test_curriculum_view_tool_compact():
    aio(curr_kb.replace_tree(_spec_two_chapters()))
    from pondr.tools.curriculum import curriculum_view
    out = aio(curriculum_view(max_depth=3))
    assert out["counts"]["solid"] == 1
    assert len(out["tree"]) == 2
    chap = out["tree"][0]
    assert chap["title"] == "經典策略"
    assert "children" in chap
    assert chap["children"][0]["title"] == "1.1 Momentum"
    # n_notes counter populated
    assert chap["children"][0]["n_notes"] == 3
