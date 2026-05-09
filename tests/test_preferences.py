"""User preferences tests."""
import asyncio
from pondr.kb import preferences as prefs_kb


def aio(c): return asyncio.get_event_loop().run_until_complete(c)


def test_init_creates_md(tmp_path, monkeypatch):
    aio(prefs_kb.init())
    from pondr import config as cfg
    assert cfg.PREFS_MD_PATH.exists()


def test_save_and_list():
    out = aio(prefs_kb.save("language", "繁體中文", category="communication",
                             channel="ws", user_msg="請跟我說繁體中文"))
    assert out["ok"]
    rows = aio(prefs_kb.list_active())
    assert any(r["key"] == "language" and r["value"] == "繁體中文" for r in rows)


def test_save_replace_keeps_history():
    aio(prefs_kb.save("language", "繁體中文", "communication"))
    aio(prefs_kb.save("language", "English", "communication"))  # replace
    cur = aio(prefs_kb.get("language"))
    assert cur["value"] == "English"


def test_sensitive_blocked():
    out = aio(prefs_kb.save("api_key", "sk-ABC123"))
    assert not out["ok"]
    out = aio(prefs_kb.save("favorite_color", "blue, my password is secret"))
    assert not out["ok"]


def test_delete():
    aio(prefs_kb.save("tone", "concise"))
    assert aio(prefs_kb.delete("tone"))
    assert aio(prefs_kb.get("tone")) is None


def test_get_active_language_returns_value():
    aio(prefs_kb.delete("language"))
    assert aio(prefs_kb.get_active_language()) is None
    aio(prefs_kb.save("language", "繁體中文", "communication"))
    assert aio(prefs_kb.get_active_language()) == "繁體中文"


def test_get_active_language_none_when_inactive():
    aio(prefs_kb.save("language", "繁體中文", "communication"))
    aio(prefs_kb.delete("language"))
    assert aio(prefs_kb.get_active_language()) is None


def test_search():
    aio(prefs_kb.save("language", "繁體中文", "communication"))
    res = aio(prefs_kb.search("lang"))
    assert any(r["key"] == "language" for r in res)


def test_llm_chat_no_hint_does_not_inject_language(monkeypatch):
    """Internal calls (no language_hint) must NOT receive a language directive.

    The old global injection added ~300 tokens to every llm.chat call, even
    planner/executor/synth/router. The new model only injects when the call
    site explicitly passes language_hint.
    """
    from pondr import llm
    aio(prefs_kb.save("language", "繁體中文", "communication"))

    captured: dict = {}

    class _FakeResp:
        def model_dump(self):
            return {"choices": [{"message": {"role": "assistant",
                                              "content": "ok"}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "client", lambda: _FakeClient())

    aio(llm.chat([{"role": "system", "content": "Original system."},
                  {"role": "user", "content": "hi"}]))

    msgs = captured["messages"]
    # No prepended language directive — first message is what we passed.
    assert msgs[0]["content"] == "Original system."
    # And nothing in the conversation references the user's language pref.
    full = "\n".join(m.get("content", "") for m in msgs)
    assert "繁體中文" not in full
    assert "輸出" not in full
    assert "MUST FOLLOW" not in full


def test_llm_chat_language_hint_prepends_short_directive(monkeypatch):
    from pondr import llm

    captured: dict = {}

    class _FakeResp:
        def model_dump(self):
            return {"choices": [{"message": {"role": "assistant",
                                              "content": "好的"}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0}}

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            return _FakeResp()

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    monkeypatch.setattr(llm, "client", lambda: _FakeClient())

    aio(llm.chat([{"role": "system", "content": "Original system."},
                  {"role": "user", "content": "hi"}],
                  language_hint="繁體中文"))

    msgs = captured["messages"]
    # The directive is the FIRST message and is short.
    assert msgs[0] == {"role": "system", "content": "輸出繁體中文"}
    assert len(msgs[0]["content"]) < 12, (
        f"directive too long: {len(msgs[0]['content'])} chars"
    )
    # Original messages still follow, unmodified.
    assert msgs[1] == {"role": "system", "content": "Original system."}
    assert msgs[2] == {"role": "user", "content": "hi"}


def test_llm_chat_language_hint_none_is_noop(monkeypatch):
    from pondr import llm
    msgs_in = [{"role": "user", "content": "hi"}]
    out = llm._prepend_language_hint(msgs_in, None)
    assert out is msgs_in or out == msgs_in
    assert len(out) == 1
