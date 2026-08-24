"""Steering a Browser Agent must not read as "stop and report".

Reported: "when I try to steer it, it stops completely and wraps up."

The transport is not what does that. steer_subagent queues the message,
_inject_steer_messages appends it after the tool results, and the loop carries
straight on to the next model call -- there is no wrapup anywhere on that path,
and these tests pin that so the next person suspecting the plumbing can stop.

What was doing it is the FRAMING. Every clause of the old template was a
prohibition -- "NOT a new task", "do not restart", "do not treat this as", "do
not expand scope" -- and a wall of don'ts arriving mid-task, with nothing
telling the model to keep acting, reads as "something is wrong, stop". For the
Browser Agent that is especially sharp: its one instruction for finishing is
"reply with NO tool calls", so stopping and reporting are the same move.
"""

import sys
import types

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.prompts import BROWSER_AGENT_SYSTEM, STEER_NUDGE_TEMPLATE


def _framed(text="use the search box, not the menu"):
    return STEER_NUDGE_TEMPLATE.format(text=text)


def test_the_users_words_are_carried_through_intact():
    assert "use the search box, not the menu" in _framed()


def test_it_says_to_keep_going():
    said = _framed().lower()
    assert "keep going" in said
    assert "using tools as before" in said


def test_it_says_outright_that_this_is_not_a_reason_to_finish():
    """The whole bug in one sentence. Without it, a correction and a
    cancellation look the same to the model."""
    said = _framed().lower()
    assert "not a signal to stop and report" in said
    assert "only finish when the original task is actually done" in said


def test_it_still_holds_the_scope():
    """The reason the framing exists at all: an unframed message mid-turn reads
    as a brand-new top-level instruction with equal weight to the original
    task, and steering used to blow past its scope entirely."""
    said = _framed().lower()
    assert "same scope" in said
    assert "don't restart" in said


def test_the_instruction_is_the_last_thing_read():
    """Same lesson as "a request ends on the turn, not on a note about it": the
    last thing in the message is the thing answered. Ending on "keep going" is
    the entire point of the reordering -- ending on the user's text invites a
    reply to the text instead of a continuation of the work."""
    out = _framed("stop using the menu")
    assert out.rstrip().endswith("]")
    assert out.index("stop using the menu") < out.index("Keep going")


def test_it_is_not_a_wall_of_prohibitions():
    """Four consecutive "do nots" is what read as "something is wrong". One
    short caution is the budget."""
    said = _framed().lower()
    assert said.count("do not") + said.count("don't") <= 2


def test_the_browser_agent_finishes_only_on_done_or_blocked():
    """The other half of the pair: this is why a "stop" reading is so easy for
    this particular sub-agent to take -- its one instruction for finishing is
    to reply with no tool calls, so stopping and reporting are the same move.

    The wording widened from "done (or blocked)" to include partly done, and
    that is the same lesson from the other side: a binary of finished-or-
    blocked has nowhere to put a partial result, so a partial rounds UP into a
    claim of success. Pinned as the property -- it reports on more than one
    outcome -- rather than as the exact phrase, which is what broke here."""
    assert "NO tool calls" in BROWSER_AGENT_SYSTEM
    for outcome in ("done", "partly done", "blocked"):
        assert outcome in BROWSER_AGENT_SYSTEM, outcome


# --------------------------------------------------------------------- #
# The transport half: steering does not stop anything.
#
# Pinned so the next person suspecting the plumbing can stop looking there.

from glmcode.agent import Agent            # noqa: E402
from glmcode.config import Config          # noqa: E402

from conftest import FakeResult, ScriptedClient, tool_call   # noqa: E402


def _agent(monkeypatch, events, script):
    import glmcode.agent as agent_mod
    monkeypatch.setattr(agent_mod, "ZaiClient", ScriptedClient)
    ScriptedClient.scripts = [script]
    return Agent(Config(), ScriptedClient(), events=events)


def test_a_steer_mid_turn_does_not_end_the_turn(monkeypatch, events):
    """It keeps calling tools afterwards. The message is appended after the
    tool results and the loop carries straight on to the next model call."""
    calls = []

    def script(n):
        calls.append(n)
        if n == 1:
            return FakeResult(tool_calls=[tool_call("c1", "list_dir", '{"path": "."}')])
        if n == 2:
            return FakeResult(tool_calls=[tool_call("c2", "list_dir", '{"path": "."}')])
        return FakeResult(content="done")

    agent = _agent(monkeypatch, events, script)
    # Queue the steer before the turn so it is waiting when the first tool
    # result comes back -- the same moment the real one is picked up.
    assert agent.steer("use the search box")
    agent.run_turn({"role": "user", "content": "go"})

    assert len(calls) >= 3, calls        # it kept working after the steer
    assert agent.messages[-1]["content"] == "done"


def test_the_steer_reaches_the_model_as_a_framed_user_message(monkeypatch, events):
    def script(n):
        if n == 1:
            return FakeResult(tool_calls=[tool_call("c1", "list_dir", '{"path": "."}')])
        return FakeResult(content="done")

    agent = _agent(monkeypatch, events, script)
    agent.steer("use the search box")
    agent.run_turn({"role": "user", "content": "go"})

    steered = [m for m in agent.messages
               if m.get("role") == "user" and "use the search box" in str(m.get("content"))]
    assert steered, agent.messages
    assert "Keep going" in steered[0]["content"]


def test_a_steer_does_not_set_the_wrapup_flag(monkeypatch, events):
    """wrap_up_requested is the ONLY thing on this path that ends a turn
    early, and steering must never touch it."""
    def script(n):
        if n == 1:
            return FakeResult(tool_calls=[tool_call("c1", "list_dir", '{"path": "."}')])
        return FakeResult(content="done")

    agent = _agent(monkeypatch, events, script)
    agent.steer("use the search box")
    assert not agent.wrap_up_requested.is_set()
    agent.run_turn({"role": "user", "content": "go"})
    assert not agent.wrap_up_requested.is_set()


def test_wrapping_up_is_a_separate_request(monkeypatch, events):
    """The two are different verbs and different buttons. This is what
    steering is NOT."""
    def script(n):
        return FakeResult(tool_calls=[tool_call(f"c{n}", "list_dir", '{"path": "."}')])

    agent = _agent(monkeypatch, events, script)
    agent.request_wrapup()
    agent.run_turn({"role": "user", "content": "go"})
    assert "(forced wrap-up report)" in str(agent.messages[-1].get("content"))


# --------------------------------------------------------------------- #
# Which model is driving
#
# "The browser agent is completely incapable" is the expected outcome of a
# small model driving a page -- the Browser Agent's own prompt says driving one
# is the hardest thing a small model does here. With no dedicated browser model
# configured it silently inherits the chat's, and the app said nothing at all:
# no way to tell a bad model from a broken feature, and the setting that fixes
# it is one nobody had a reason to look for.

def test_it_names_the_chats_model_and_where_to_change_it(monkeypatch, events):
    agent = _agent(monkeypatch, events, lambda n: FakeResult(content="x"))
    agent._say_browser_model(None)
    said = " ".join(t for _lvl, t in events.notices)
    assert "Browser Agent" in said
    assert "Settings" in said and "Browser model" in said


def test_a_dedicated_model_is_named_without_the_advice(monkeypatch, events):
    agent = _agent(monkeypatch, events, lambda n: FakeResult(content="x"))
    agent._say_browser_model("qwen2.5-coder:32b")
    said = " ".join(t for _lvl, t in events.notices)
    assert "qwen2.5-coder:32b" in said
    assert "Settings" not in said       # nothing to fix; do not nag


def test_it_is_said_once_per_chat_not_once_per_run(monkeypatch, events):
    """News the first time, noise after that -- the same rule the rate-limit
    fallback notice follows."""
    agent = _agent(monkeypatch, events, lambda n: FakeResult(content="x"))
    for _ in range(4):
        agent._say_browser_model(None)
    said = [t for _lvl, t in events.notices]
    assert len([t for t in said if "Browser Agent" in t]) == 1


# --------------------------------------------------------------------- #
# The wrapping and the unwrapping have to stay in step
#
# Found by the suite, not by inspection: moving the framing AFTER the user's
# words broke sessions.to_display, which stripped only the FRONT of the
# template. Every steer note in a replayed chat would have carried the whole
# instruction block hanging off the end of it.

def test_a_steer_note_renders_as_just_what_the_user_typed():
    from glmcode.sessions import to_display
    msgs = [
        {"role": "user", "content": "refactor auth"},
        {"role": "assistant", "content": "on it"},
        {"role": "user", "content": STEER_NUDGE_TEMPLATE.format(
            text="also check the tests folder")},
        {"role": "assistant", "content": "will do"},
    ]
    steered = [it["text"] for it in to_display(msgs) if it["kind"] == "steered"]
    assert steered == ["also check the tests folder"]


def test_the_unwrapping_is_derived_from_the_template():
    """Not written out a second time. The prefix always was; the suffix is
    what the reordering caught out, so both halves come from one split now and
    neither can drift from the framing again."""
    from glmcode import sessions
    prefix, _, suffix = STEER_NUDGE_TEMPLATE.partition("{text}")
    assert sessions._STEER_PREFIX == prefix
    assert sessions._STEER_SUFFIX == suffix


def test_no_framing_survives_into_the_rendered_note():
    """Whichever side it sits on. A future template could put framing back in
    front, or on both sides, and this still holds."""
    from glmcode.sessions import to_display
    msgs = [{"role": "user", "content": STEER_NUDGE_TEMPLATE.format(text="use search")}]
    note = [it["text"] for it in to_display(msgs) if it["kind"] == "steered"][0]
    assert note == "use search"
    for word in ("Keep going", "Steering tip", "["):
        assert word not in note
