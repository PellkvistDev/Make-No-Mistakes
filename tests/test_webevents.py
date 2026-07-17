"""WebEvents IPC batching: every evaluate_js call is a synchronous, blocking
round trip through the WebView2 UI thread, so high-frequency events (token
deltas -- especially from parallel sub-agents) must be buffered and flushed
in batches, never sent one call per token."""

import json
import sys
import types

import pytest

# glmcode.gui.app imports `webview` at module level; stub it so these tests
# run anywhere (CI has no pywebview).
sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object()))

from glmcode.gui.app import WebEvents  # noqa: E402


class FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


def make_events():
    ev = WebEvents()
    ev._window = FakeWindow()
    ev._ensure_flush_thread = lambda: None  # deterministic: flush manually
    return ev


def payloads(ev):
    out = []
    for s in ev._window.scripts:
        out.append(json.loads(s[s.index("(") + 1:s.rindex(")")]))
    return out


def test_subagent_text_deltas_are_batched_not_per_token():
    ev = make_events()
    for i in range(200):
        ev.subagent_stream("sa1", "reasoning", text=f"r{i} ")
        ev.subagent_stream("sa1", "content", text=f"c{i} ")
    # Nothing crossed the IPC boundary yet -- 400 events, zero round trips.
    assert ev._window.scripts == []
    ev._flush_stream_buffers()
    ps = payloads(ev)
    # One batched reasoning event + one batched content event.
    assert len(ps) == 2
    kinds = {p["kind"]: p for p in ps}
    assert kinds["reasoning"]["text"].startswith("r0 ") and "r199 " in kinds["reasoning"]["text"]
    assert kinds["content"]["text"].startswith("c0 ") and "c199 " in kinds["content"]["text"]


def test_multiple_subagents_buffer_independently():
    ev = make_events()
    ev.subagent_stream("sa1", "content", text="one")
    ev.subagent_stream("sa2", "content", text="two")
    ev._flush_stream_buffers()
    ps = payloads(ev)
    by_aid = {p["id"]: p["text"] for p in ps}
    assert by_aid == {"sa1": "one", "sa2": "two"}


def test_non_text_event_flushes_buffered_text_first():
    # A tool_call must never overtake the content that streamed before it.
    ev = make_events()
    ev.subagent_stream("sa1", "content", text="thinking about it... ")
    ev.subagent_stream("sa1", "tool_call", name="read_file", args={"path": "x"})
    ps = payloads(ev)
    assert [p["kind"] for p in ps] == ["content", "tool_call"]
    assert ps[0]["text"] == "thinking about it... "


def test_subagent_tool_result_truncated_for_display():
    ev = make_events()
    ev.subagent_stream("sa1", "tool_result", name="read_file",
                       content="x" * 60_000, is_error=False)
    ps = payloads(ev)
    assert len(ps) == 1
    assert len(ps[0]["content"]) == 12_000


def test_main_agent_deltas_batched_and_stream_end_flushes():
    ev = make_events()
    for i in range(100):
        ev.content_delta(f"t{i} ")
    assert ev._window.scripts == []  # buffered
    ev.stream_end()
    ps = payloads(ev)
    assert [p["type"] for p in ps] == ["content", "stream_end"]
    assert ps[0]["text"].startswith("t0 ") and "t99 " in ps[0]["text"]


def test_main_tool_result_truncated_for_display():
    ev = make_events()
    ev.tool_result("read_file", "y" * 60_000)
    ps = payloads(ev)
    assert len(ps[0]["content"]) == 12_000


def test_events_tagged_with_session_id():
    ev = WebEvents("chat-42")
    ev._window = FakeWindow()
    ev._ensure_flush_thread = lambda: None
    ev.tool_call("read_file", {"path": "x"})
    ev.subagent_stream("sa1", "tool_call", name="grep", args={})
    for p in payloads(ev):
        assert p["sid"] == "chat-42"


def test_sidless_events_stay_untagged():
    ev = make_events()  # no sid (global sink)
    ev.tool_call("read_file", {})
    assert "sid" not in payloads(ev)[0]


def test_permission_registry_shared_across_chats():
    import threading
    shared = {}
    ev_a = WebEvents("chat-a", shared)
    ev_b = WebEvents("chat-b", shared)
    ev_a._window = FakeWindow()
    ev_b._window = FakeWindow()
    answers = {}

    def ask():
        answers["got"] = ev_a.ask_permission("t", "p")

    t = threading.Thread(target=ask, daemon=True)  # daemon: a regression here must fail, not hang pytest
    t.start()
    # wait for chat A's prompt to register, then answer it THROUGH chat B's
    # sink -- the registry is shared, so any sink can resolve any prompt
    for _ in range(100):
        if shared:
            break
        threading.Event().wait(0.01)
    rid = next(iter(shared))
    ev_b.resolve_permission(rid, "y")
    t.join(timeout=5)
    assert answers["got"] == "y"
