"""A worker dispatched by voice reports back to the agent you type to.

Work started by speaking used to land nowhere you could ask about it. The
delegator heard the result -- it ran the announcement turn -- but the delegator
is a separate, voice-only agent. Close the overlay, type "what did that worker
change?", and the coding agent had never been told anything happened.

The report is queued rather than appended straight to agent.messages, and that
is the whole design: a worker finishes on its own daemon thread, at a moment
nothing controls, and the coding agent may be mid-turn. Writing into its history
underneath it is how you get a tool_call with no matching reply -- the exact
shape OpenAI-compatible APIs reject outright, and the same failure
heal_interrupted_turn exists to repair on the sync path.
"""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402
from glmcode.prompts import WORKER_REPORT_PREFIX, worker_report_note  # noqa: E402
from glmcode.sessions import to_display  # noqa: E402


class _Agent:
    def __init__(self):
        self.messages = []


def _chat():
    cs = gui_app.ChatState.__new__(gui_app.ChatState)
    import threading
    cs.agent = _Agent()
    cs.convo_agent = _Agent()
    cs.worker_reports = []
    cs.worker_reports_lock = threading.Lock()
    return cs


def _api(active=None):
    """_active is a read-only property over _chats/session_id, so the chat is
    installed the way the app installs one rather than by assignment."""
    api = gui_app.Api.__new__(gui_app.Api)
    api._chats = {}
    api.session_id = ""
    if active is not None:
        api._chats["s1"] = active
        api.session_id = "s1"
    return api


# ------------------------------------------------------------ the note ----

def test_the_note_carries_the_worker_name_and_its_output():
    note = worker_report_note("dark-mode", "done", "Edited style.css and app.js")
    assert "dark-mode" in note
    assert "Edited style.css and app.js" in note
    assert note.startswith(WORKER_REPORT_PREFIX)


def test_a_failed_worker_is_not_described_as_finished():
    """The one thing worse than losing the report is filing a wrong one."""
    note = worker_report_note("flaky", "error", "TypeError on line 12")
    assert "failed" in note
    assert "finished successfully" not in note


def test_the_note_says_it_is_not_an_instruction():
    """It arrives in the user role, which is where instructions come from.
    Nothing else in the message distinguishes a record from a request."""
    note = worker_report_note("w", "done", "did the thing")
    assert "not an instruction" in note or "context, not an instruction" in note


def test_an_empty_result_still_produces_a_usable_note():
    assert "(no output)" in worker_report_note("quiet", "done", "")


# ------------------------------------------------------ queue and drain ---

def test_a_report_reaches_the_coding_agent_on_its_next_turn():
    api, cs = _api(), _chat()
    api._queue_worker_report(cs, worker_report_note("tidy", "done", "tidied it"))

    assert cs.agent.messages == [], "nothing may be written before the turn starts"
    api._drain_worker_reports(cs)

    assert len(cs.agent.messages) == 1
    assert "tidied it" in cs.agent.messages[0]["content"]
    assert cs.agent.messages[0]["role"] == "user"


def test_draining_twice_does_not_repeat_the_report():
    api, cs = _api(), _chat()
    api._queue_worker_report(cs, worker_report_note("w", "done", "x"))
    api._drain_worker_reports(cs)
    api._drain_worker_reports(cs)
    assert len(cs.agent.messages) == 1


def test_reports_arrive_in_the_order_the_workers_finished():
    api, cs = _api(), _chat()
    for n in ("first", "second", "third"):
        api._queue_worker_report(cs, worker_report_note(n, "done", n + " output"))
    api._drain_worker_reports(cs)
    assert [m["content"].split("'")[1] for m in cs.agent.messages] == \
        ["first", "second", "third"]


def test_the_queue_is_bounded():
    """A long voice session can dispatch a lot of workers, and every queued
    report is spent from the coding agent's context the moment it next runs."""
    api, cs = _api(), _chat()
    for i in range(50):
        api._queue_worker_report(cs, worker_report_note(f"w{i}", "done", "x"))
    api._drain_worker_reports(cs)
    assert len(cs.agent.messages) == 20
    # The ones kept are the most recent, not the oldest.
    assert "'w49'" in cs.agent.messages[-1]["content"]


# --------------------------------------------------- both voice engines ---

def test_recording_a_result_reaches_both_agents():
    """The live engine says the announcement itself, so it calls this instead
    of announce_worker -- but the report still has to land in both histories."""
    cs = _chat()
    api = _api(cs)

    assert api.record_worker_result("tidy", "done", "renamed things")["ok"] is True

    assert any("renamed things" in m["content"] for m in cs.convo_agent.messages), \
        "the delegator needs it to answer a follow-up question out loud"
    api._drain_worker_reports(cs)
    assert any("renamed things" in m["content"] for m in cs.agent.messages), \
        "the coding agent needs it so you can ask by typing"


def test_recording_without_an_open_chat_is_not_an_error():
    assert _api().record_worker_result("w", "done", "x") == {"ok": False}


def test_a_chat_with_no_voice_session_still_files_the_report():
    """Workers outlive the overlay: closing voice mode drops convo_agent, and
    the report must not go with it."""
    cs = _chat()
    cs.convo_agent = None
    api = _api(cs)
    api.record_worker_result("w", "done", "still mattered")
    api._drain_worker_reports(cs)
    assert any("still mattered" in m["content"] for m in cs.agent.messages)


# ------------------------------------------------------------- display ---

def test_the_report_is_not_rendered_as_something_the_user_typed():
    """It goes in the user role because that is the role this app injects
    plumbing under. Replay must not show it as a message you sent."""
    note = worker_report_note("w", "done", "output here")
    shown = to_display([{"role": "user", "content": note}])
    assert shown == [], f"the report rendered as a user message: {shown}"
