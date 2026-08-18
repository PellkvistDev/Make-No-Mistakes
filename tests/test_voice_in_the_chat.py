"""A spoken exchange belongs in the chat, not only in a log beside it.

Voice mode used to leave no trace in the conversation you were looking at. You
could talk for ten minutes, close the overlay, and the window still showed
whatever had been typed before -- and the coding agent, asked "what did we just
decide?", had never heard any of it. The exchange went to the append-only
transcript file and nowhere else.

The phone has always done the other thing: voiceRecordTurn pushes both halves
straight into session.messages and renders them as bubbles. Moving between
talking and typing should not be a change of subject, so the desktop now does
the same.

The safety rule is the one the worker reports already follow: appending to
agent.messages underneath a running turn produces a tool_call with no matching
reply, which OpenAI-compatible APIs reject outright. So a busy chat queues.
"""

import sys
import threading
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402


class _Agent:
    def __init__(self):
        self.messages = []
        self.transcript = None
        self.workdir = "."
        self.session_usage = types.SimpleNamespace(prompt_tokens=0, completion_tokens=0)
        self.todos = []


class _Events:
    def __init__(self):
        self.emitted = []

    def emit(self, type_, **data):
        self.emitted.append((type_, data))


def _chat(convo_reply=""):
    cs = gui_app.ChatState.__new__(gui_app.ChatState)
    cs.sid = "s1"
    cs.agent = _Agent()
    cs.events = _Events()
    cs.title = ""
    cs.provider = cs.model = ""
    cs.auto_backup = True
    cs.turn_snapshots = []
    cs.turn_lock = threading.Lock()
    cs.convo_agent = _Agent()
    if convo_reply:
        cs.convo_agent.messages.append({"role": "assistant", "content": convo_reply})
    cs.voice_turns = []
    cs.voice_turns_lock = threading.Lock()
    return cs


def _api(saved=None):
    api = gui_app.Api.__new__(gui_app.Api)
    # `saved if saved is not None` -- an EMPTY list is falsy, so `saved or []`
    # would quietly append to a throwaway and the test would claim nothing was
    # saved when it had been.
    sink = saved if saved is not None else []
    api._store = types.SimpleNamespace(save=lambda *a, **k: sink.append(a))
    return api


# ------------------------------------------------- it lands in the chat ---

def test_a_spoken_exchange_becomes_chat_messages():
    api, cs = _api(), _chat(convo_reply="I renamed the handler.")
    api._persist_voice_turn(cs, "rename the click handler")

    assert [(m["role"], m["content"]) for m in cs.agent.messages] == [
        ("user", "rename the click handler"),
        ("assistant", "I renamed the handler."),
    ]


def test_the_chat_window_is_told_so_it_can_render_them():
    """The messages exist for the model; the event is what puts them on
    screen while the overlay is still open."""
    api, cs = _api(), _chat(convo_reply="Done.")
    api._persist_voice_turn(cs, "do the thing")

    kinds = [t for t, _ in cs.events.emitted]
    assert "voice_chat_turn" in kinds
    _, data = next(e for e in cs.events.emitted if e[0] == "voice_chat_turn")
    assert data["user"] == "do the thing"
    assert data["assistant"] == "Done."


def test_it_is_announced_on_the_coding_chat_not_the_overlay():
    """cs.events is the coding chat's sink. The voice sid drives the overlay,
    which is not where a chat message belongs."""
    api, cs = _api(), _chat(convo_reply="ok")
    api._persist_voice_turn(cs, "hello")
    assert cs.events.emitted, "nothing was emitted on the coding chat's sink"


def test_the_chat_is_saved_so_it_survives_a_reload():
    saved = []
    api, cs = _api(saved), _chat(convo_reply="Done.")
    api._persist_voice_turn(cs, "do it")
    assert saved, "a spoken exchange that is never saved is lost on restart"


# --------------------------------------------- and never mid-turn ---------

def test_a_busy_chat_queues_instead_of_writing_underneath_the_turn():
    """The rule the worker reports already follow. Writing into a running
    agent's history is how you get a tool_call with no matching reply."""
    api, cs = _api(), _chat(convo_reply="Done.")
    cs.turn_lock.acquire()
    try:
        api._persist_voice_turn(cs, "said while it was working")
        assert cs.agent.messages == [], "it wrote into a running turn"
        assert len(cs.voice_turns) == 1
    finally:
        cs.turn_lock.release()


def test_the_queue_is_handed_over_on_the_next_turn():
    api, cs = _api(), _chat(convo_reply="Done.")
    cs.turn_lock.acquire()
    try:
        api._persist_voice_turn(cs, "queued")
    finally:
        cs.turn_lock.release()

    api._drain_voice_turns(cs)

    assert [m["content"] for m in cs.agent.messages] == ["queued", "Done."]
    assert cs.voice_turns == []


def test_draining_twice_does_not_repeat_the_exchange():
    api, cs = _api(), _chat(convo_reply="Done.")
    cs.turn_lock.acquire()
    try:
        api._persist_voice_turn(cs, "queued")
    finally:
        cs.turn_lock.release()
    api._drain_voice_turns(cs)
    api._drain_voice_turns(cs)
    assert len(cs.agent.messages) == 2


def test_the_queue_is_bounded():
    api, cs = _api(), _chat(convo_reply="Done.")
    cs.turn_lock.acquire()
    try:
        for i in range(60):
            api._persist_voice_turn(cs, f"turn {i}")
    finally:
        cs.turn_lock.release()
    assert len(cs.voice_turns) == 40
    assert cs.voice_turns[-1]["user"] == "turn 59"


# ------------------------------------- whose transcription is recorded ----

def test_the_live_engine_s_own_transcript_is_used_verbatim():
    """Gemini transcribes both directions and hands them over. Digging the
    reply back out of the delegator's history is a copy of it at best, and
    absent entirely when there is no delegator."""
    api, cs = _api(), _chat(convo_reply="something stale")
    api._persist_voice_turn(cs, "what I said", reply="what Gemini said")

    assert [m["content"] for m in cs.agent.messages] == \
        ["what I said", "what Gemini said"]


def test_the_local_engine_falls_back_to_the_delegator_s_reply():
    """There is no Gemini in the local path -- Whisper hears, the convo agent
    answers -- so the reply has to come from its history."""
    api, cs = _api(), _chat(convo_reply="the local reply")
    api._persist_voice_turn(cs, "spoken words")
    assert cs.agent.messages[-1]["content"] == "the local reply"


def test_a_missing_delegator_does_not_lose_the_user_s_words():
    """Closing voice mode drops convo_agent. What was said still happened."""
    api, cs = _api(), _chat()
    cs.convo_agent = None
    api._persist_voice_turn(cs, "still said this")
    assert cs.agent.messages[0]["content"] == "still said this"


def test_an_empty_exchange_adds_nothing():
    api, cs = _api(), _chat()
    api._persist_voice_turn(cs, "", reply="")
    assert cs.agent.messages == []
    assert cs.events.emitted == []
