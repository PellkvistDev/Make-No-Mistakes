"""Read-aloud routing: the main chat's replies are read as before, and now a
sub-agent (or Browser Agent) is ALSO read aloud -- but only while its
inspector panel is the one currently focused (set_active_view), since the
main chat sits silently waiting on it in the meantime anyway. Reasoning is
never spoken, for either source, matching the pre-existing main-agent
behavior."""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui.app import Api, ChatState, WebEvents  # noqa: E402


def make_events():
    ev = WebEvents()
    ev._ensure_flush_thread = lambda: None    # deterministic: no background flush
    ev._ensure_tts_worker = lambda: None       # deterministic: inspect the queue directly
    ev.read_aloud_this_turn = True
    return ev


def queued_texts(ev) -> list[str]:
    out = []
    while not ev._tts_queue.empty():
        seq, text = ev._tts_queue.get_nowait()
        out.append(text)
    return out


# A single sentence comfortably over the 40-char chunk floor.
SENTENCE = "This is a complete sentence that is long enough to flush."


def test_main_content_unaffected_by_default_active_view():
    ev = make_events()
    ev.content_delta(SENTENCE + " ")
    assert queued_texts(ev) == [SENTENCE]   # unchanged from before this feature
    ev2 = make_events()
    ev2.read_aloud_this_turn = False
    ev2.content_delta(SENTENCE + " ")
    assert queued_texts(ev2) == []          # toggle off -> nothing queued


def test_subagent_content_is_silent_until_its_view_is_focused():
    ev = make_events()
    ev.subagent_stream("sa1", "content", text=SENTENCE + " ")
    assert queued_texts(ev) == []    # nobody is watching sa1 -- stays silent


def test_subagent_content_speaks_once_its_view_is_active():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text=SENTENCE + " ")
    assert queued_texts(ev) == [SENTENCE]


def test_only_the_focused_subagent_speaks_not_others():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa2", "content", text=SENTENCE + " ")  # watching sa1, not sa2
    assert queued_texts(ev) == []
    ev.subagent_stream("sa1", "content", text=SENTENCE + " ")
    assert queued_texts(ev) == [SENTENCE]


def test_reasoning_is_never_spoken_for_subagents_either():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "reasoning", text=SENTENCE + " ")
    assert queued_texts(ev) == []


def test_switching_view_drops_the_old_partial_buffer():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="Short thought, ")  # too short to flush yet
    assert queued_texts(ev) == []
    ev.set_active_view("sa2")   # user looked away mid-sentence
    ev.subagent_stream("sa1", "stream_end")  # sa1 finishes in the background
    assert queued_texts(ev) == []            # its half-sentence is never spoken
    # sa2's feeder is genuinely fresh, not polluted by sa1's leftovers
    ev.subagent_stream("sa2", "content", text=SENTENCE + " ")
    assert queued_texts(ev) == [SENTENCE]


def test_stream_end_flushes_trailing_text_only_for_the_active_view():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="Too short to auto-flush")
    assert queued_texts(ev) == []
    ev.subagent_stream("sa1", "stream_end")
    assert queued_texts(ev) == ["Too short to auto-flush"]

    # A non-active sub-agent's stream_end never touches the (shared) buffer.
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="Still buffering this")
    ev.subagent_stream("sa9", "stream_end")   # some other, unwatched agent finishes
    assert queued_texts(ev) == []             # sa1's partial text is untouched
    ev.subagent_stream("sa1", "stream_end")
    assert queued_texts(ev) == ["Still buffering this"]


def test_stream_start_resets_only_the_active_views_buffer():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="Old partial sentence")
    ev.subagent_stream("sa1", "stream_start")  # sa1 begins a fresh round
    ev.subagent_stream("sa1", "stream_end")
    assert queued_texts(ev) == []   # the old partial text was discarded, not spoken


def test_set_active_view_is_a_noop_when_unchanged():
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="Part one, ")
    ev.set_active_view("sa1")   # redundant re-focus (e.g. re-clicking the same tab)
    ev.subagent_stream("sa1", "content", text="part two of the same sentence.")
    assert queued_texts(ev) == ["Part one, part two of the same sentence."]


def test_display_batching_for_subagent_text_still_works_alongside_tts():
    """The pre-existing visual-panel batching (_sub_bufs) is unaffected by
    the new TTS routing riding along the same content deltas."""
    ev = make_events()
    ev.set_active_view("sa1")
    ev.subagent_stream("sa1", "content", text="hello ")
    with ev._stream_lock:
        assert ev._sub_bufs["sa1"]["content"] == "hello "


def test_api_set_active_view_routes_to_the_active_chats_events():
    api = Api.__new__(Api)
    ev = WebEvents("s1")
    api._chats = {"s1": ChatState("s1", None, ev)}
    api.session_id = "s1"
    res = api.set_active_view("sa7")
    assert res == {"ok": True}
    assert ev.active_view == "sa7"
    res = api.set_active_view("")   # closing the panel goes back to main
    assert res == {"ok": True}
    assert ev.active_view == ""
