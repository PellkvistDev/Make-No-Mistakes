"""A chat has two agents and one set of workers.

Reported as "there are problems with the connection between the voice agents
and the text agents -- no context works", and it was literal: `_workers` is an
instance attribute, and a chat builds TWO Agents -- the one you type to and the
delegator you speak to. Each kept its own registry.

So work started by speaking was invisible to `check_workers` typed into the
same chat, and `steer_worker`, `stop_worker`, `worker_changes` and
`revert_worker` all answered "no worker matches" for it. The reverse held too.

What made it worse rather than merely incomplete: the RESULT already crossed
over (worker_reports), so the coding agent was told a worker had finished and
then could not find the worker it had just been told about.

The second half of the report -- "when the text agent spawns agents, it doesn't
work" -- is the other test here. A sub-agent's event sink inherited
AgentEvents, whose ask_permission refuses with "no frontend attached to approve
this": a message written for the CLI, arriving in an app that has one. In the
default 'ask' mode that denied every write a spawned sub-agent tried, silently.
"""

import threading
import time
import types

import pytest

from glmcode.agent import Agent, _CaptureEvents
from glmcode.permissions import PermissionEngine
from glmcode.tools import ToolError

from pathlib import Path


def _agent(conversational=False):
    """A coordinator with the model and the browser faked out, but the real
    registry, the real id allocation and the real worker thread."""
    ag = Agent.__new__(Agent)
    ag.allow_subagents = True
    ag.conversational = conversational
    ag.backup_repo = None
    ag._workers, ag._workers_lock = {}, threading.Lock()
    ag._worker_seq = 0
    ag._worker_limiter = None
    ag._worker_perms, ag._worker_perms_lock = {}, threading.Lock()
    ag._emit_lock = threading.Lock()
    ag._active_subagents, ag._active_subagents_lock = {}, threading.Lock()
    # request_cancel reaches the tool the turn is blocked on, and reads this to
    # do it. Set here rather than defended against in the agent: a real Agent
    # always has it from __init__, and a getattr default would hide the day
    # that stops being true.
    ag._current_call_token = None
    ag.session_usage = types.SimpleNamespace(add=lambda u: None)
    ag.events = types.SimpleNamespace(worker_update=lambda *a, **k: None,
                                      subagent=lambda *a, **k: None,
                                      worker_permission=lambda *a, **k: None)
    ag._emit_subagent = lambda *a, **k: None
    ag._worker_changed_files = lambda wid: []
    ag._describe_changes = lambda c: ""
    ag._run_single_subagent = lambda n, t, l, w: ("did the thing", None)
    return ag


def _pair():
    """The two agents of one chat, wired the way _ensure_convo wires them."""
    coding = _agent()
    convo = _agent(conversational=True)
    convo.adopt_workers_of(coding)
    return coding, convo


def _settle():
    # The worker runs on its own daemon thread; the fake mission returns at
    # once, so this is only waiting for the thread to be scheduled.
    for _ in range(100):
        time.sleep(0.01)


# ------------------------------------------------- one registry, two ends --

def test_a_worker_started_by_speaking_is_visible_to_the_agent_you_type_to():
    coding, convo = _pair()
    convo._dispatch_worker("dark-mode", "add a dark mode")
    _settle()
    assert "dark-mode" in coding._check_workers()


def test_a_worker_started_by_typing_is_visible_to_the_one_you_speak_to():
    """The direction nobody thinks to check, and the one that makes "how is it
    going?" answerable after you have switched to talking."""
    coding, convo = _pair()
    coding._dispatch_worker("typed-one", "do the typed thing")
    _settle()
    assert "typed-one" in convo._check_workers()


def test_the_two_ends_do_not_hand_out_the_same_id():
    """Each agent keeps its own counter, so the number alone is not unique --
    without the walk in _dispatch_worker the second dispatch would overwrite
    the first one's record in the shared registry."""
    coding, convo = _pair()
    convo._dispatch_worker("first", "a")
    coding._dispatch_worker("second", "b")
    _settle()
    assert len(coding._workers) == 2
    assert sorted(coding._workers) == ["wk1", "wk2"]
    names = {w["name"] for w in coding._workers.values()}
    assert names == {"first", "second"}


def test_a_spoken_worker_can_be_stopped_by_typing():
    """Visible-but-un-steerable is a worse failure than invisible: it looks
    like it worked. Steering and stopping reach the worker through
    _active_subagents, so that has to travel with the registry."""
    coding, convo = _pair()
    started, release = threading.Event(), threading.Event()

    def slow(name, task, limiter, wid):
        started.set()
        release.wait(5)
        return "stopped early", None

    convo._run_single_subagent = slow
    convo._dispatch_worker("long-one", "something slow")
    assert started.wait(5)
    try:
        out = coding._stop_worker_tool("long-one")
        assert "long-one" in out
        assert coding._workers["wk1"]["status"] == "stopped"
    finally:
        release.set()


def test_steering_a_spoken_worker_by_typing_reaches_it():
    coding, convo = _pair()
    started, release = threading.Event(), threading.Event()
    sub = types.SimpleNamespace(steered=[], cancel=threading.Event())
    sub.steer = lambda text: sub.steered.append(text) or True

    def slow(name, task, limiter, wid):
        with convo._active_subagents_lock:
            convo._active_subagents[wid] = sub
        started.set()
        release.wait(5)
        return "done", None

    convo._run_single_subagent = slow
    convo._dispatch_worker("long-one", "something slow")
    assert started.wait(5)
    try:
        coding._steer_worker_tool("long-one", "use CSS variables")
        assert sub.steered == ["use CSS variables"]
    finally:
        release.set()


def test_nothing_else_is_shared():
    """messages, events and the model are what make one of these a spoken
    conversation and the other a written one."""
    coding, convo = _pair()
    coding.messages, convo.messages = [], []
    coding.messages.append({"role": "user", "content": "typed"})
    assert convo.messages == []
    assert coding.events is not convo.events


def test_pending_permission_cards_are_not_pooled():
    """The first version of adopt_workers_of shared these too, and two callers
    release them in bulk meaning only their own: closing voice mode denies the
    delegator's, cancelling a turn denies the coding agent's. Pooled, stopping
    a typed turn would silently deny a card a spoken worker was waiting on."""
    coding, convo = _pair()
    assert coding._worker_perms is not convo._worker_perms

    ev = threading.Event()
    convo._worker_perms["r1"] = {"event": ev, "answer": ("y", "")}
    coding.cancel = threading.Event()
    coding.request_cancel()
    assert not ev.is_set(), "stopping a typed turn denied a spoken worker's card"
    assert convo._worker_perms["r1"]["answer"] == ("y", "")


def test_one_answer_finds_the_agent_holding_it():
    """Not pooling them means the frontend's rid has to be tried against both
    -- which is what Api.resolve_worker_permission does."""
    coding, convo = _pair()
    ev = threading.Event()
    convo._worker_perms["r1"] = {"event": ev, "answer": ("n", "")}
    assert coding.resolve_worker_permission("r1", "y") is False
    assert convo.resolve_worker_permission("r1", "y") is True
    assert ev.is_set()


# --------------------------------------- a spawned sub-agent can ask -------

def _decide(agent, mode, tool, args, aid="sa1", name="research"):
    """Run one gated action through the real permission engine and the real
    sub-agent sink, and report what the sub-agent was told."""
    ask = lambda t, p, a: agent._worker_ask(aid, t, p, a, display=name)  # noqa: E731
    sink = _CaptureEvents(forward=lambda *a, **k: None, aid=aid, ask=ask)
    pe = PermissionEngine(mode=mode, path_rules=[], workdir=Path("."))
    out = {}
    t = threading.Thread(
        target=lambda: out.update(d=pe.check(tool, args, sink.ask_permission)),
        daemon=True)
    t.start()
    return out, t


def test_a_spawned_subagents_write_is_asked_about_not_silently_denied():
    """The whole of "when the text agent spawns agents, it doesn't work"."""
    ag = _agent()
    asked = []
    ag.events = types.SimpleNamespace(
        worker_permission=lambda rid, worker, title, preview, spoken="", always="":
            asked.append({"rid": rid, "worker": worker, "spoken": spoken}))
    out, t = _decide(ag, "ask", "write_file", {"path": "x.py", "content": "hi"})
    for _ in range(200):
        if asked:
            break
        time.sleep(0.01)
    assert asked, "the sub-agent was refused without anyone being asked"
    ag.resolve_worker_permission(asked[0]["rid"], "y")
    t.join(5)
    assert out["d"].allowed is True


def test_the_card_names_the_subagent_rather_than_its_id():
    """A spawn_agents sub-agent has an id like "sa9f3c21-2" and no entry in the
    worker registry, so looking its name up there would put that on the card."""
    ag = _agent()
    asked = []
    ag.events = types.SimpleNamespace(
        worker_permission=lambda rid, worker, title, preview, spoken="", always="":
            asked.append(worker))
    out, t = _decide(ag, "ask", "write_file", {"path": "x.py", "content": "hi"},
                     aid="sa9f3c21-2", name="research")
    for _ in range(200):
        if asked:
            break
        time.sleep(0.01)
    ag.deny_pending_worker_permissions("test over")
    t.join(5)
    assert asked == ["research"], "the card showed the raw sub-agent id"


def test_a_typed_spawn_does_not_start_talking():
    """worker_permission queues its sentence for TTS. Sending one from a typed
    spawn would have the app speak at someone who never opened voice mode."""
    ag = _agent(conversational=False)
    said = []
    ag.events = types.SimpleNamespace(
        worker_permission=lambda rid, worker, title, preview, spoken="", always="":
            said.append(spoken))
    ag._workers["wk1"] = {"name": "w"}
    t = threading.Thread(target=lambda: ag._worker_ask("wk1", "write x.py", "…", ""),
                         daemon=True)
    t.start()
    time.sleep(0.2)
    ag.deny_pending_worker_permissions("done")
    t.join(5)
    assert said == [""], said


def test_a_spoken_worker_still_speaks_its_question():
    ag = _agent(conversational=True)
    said = []
    ag.events = types.SimpleNamespace(
        worker_permission=lambda rid, worker, title, preview, spoken="", always="":
            said.append(spoken))
    ag._workers["wk1"] = {"name": "dark-mode"}
    t = threading.Thread(target=lambda: ag._worker_ask("wk1", "write x.py", "…", ""),
                         daemon=True)
    t.start()
    time.sleep(0.2)
    ag.deny_pending_worker_permissions("done")
    t.join(5)
    assert said and "dark-mode" in said[0]


def test_cancelling_a_turn_releases_a_subagent_blocked_on_the_card():
    """spawn_agents JOINS its threads, and a blocked ask waits five minutes.
    Without this a cancelled turn sits there with the app showing a stopped
    turn that has plainly not stopped."""
    ag = _agent()
    ag.cancel = threading.Event()
    ag.events = types.SimpleNamespace(worker_permission=lambda *a, **k: None)
    ag._workers["wk1"] = {"name": "w"}
    answer = {}
    t = threading.Thread(
        target=lambda: answer.update(a=ag._worker_ask("wk1", "write x.py", "…", "")),
        daemon=True)
    t.start()
    time.sleep(0.2)
    ag.request_cancel()
    t.join(5)
    assert not t.is_alive(), "the sub-agent is still parked on the permission card"
    assert answer["a"][0] == "n"
