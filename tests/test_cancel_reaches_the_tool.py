"""Stop has to reach what the turn is actually waiting on.

Reported: "interrupting is not reliable, especially when it's using tools,
they can't be interrupted."

Exactly right, and the reason is that `self.cancel` is a flag checked BETWEEN
steps and BETWEEN tool calls. That stops a turn which is thinking, at once, and
does nothing whatever for a turn blocked inside a tool -- which is most of the
time a turn is slow enough to want stopping.

Three things a turn blocks on, none of which watch a flag:

  - a sub-agent, because spawn_agents and an inline control_chrome JOIN their
    threads, and a sub-agent has its OWN cancel Event;
  - a shell command, because it is a process and the thing that stops a
    process is killing its tree;
  - a permission card, which is an Event with a five-minute timeout (already
    handled, and pinned here so it stays that way).
"""

import threading
import time
import types
from collections import OrderedDict

import pytest

from glmcode import tools
from glmcode.agent import Agent


def _agent():
    ag = Agent.__new__(Agent)
    ag.cancel = threading.Event()
    ag.allow_subagents = True
    ag.conversational = False
    ag._workers, ag._workers_lock = {}, threading.Lock()
    ag._worker_perms, ag._worker_perms_lock = {}, threading.Lock()
    ag._active_subagents, ag._active_subagents_lock = {}, threading.Lock()
    ag._finished_agents, ag._finished_lock = OrderedDict(), threading.Lock()
    ag._current_call_token = None
    return ag


# ------------------------------------------------- sub-agents ------------

def test_cancelling_a_turn_cancels_the_sub_agents_it_is_waiting_for():
    """spawn_agents joins. Cancelling only the coordinator left Stop waiting
    for up to MAX_SUBAGENTS missions to each run to completion."""
    parent = _agent()
    kids = [_agent() for _ in range(3)]
    for i, k in enumerate(kids):
        parent._active_subagents[f"sa{i}"] = k

    parent.request_cancel()
    assert all(k.cancel.is_set() for k in kids)


def test_it_reaches_a_sub_agents_own_sub_agent():
    """A browser agent inside a worker is two levels down, and the recursion
    is what makes Stop mean stop rather than stop-eventually."""
    parent, mid, leaf = _agent(), _agent(), _agent()
    parent._active_subagents["sa1"] = mid
    mid._active_subagents["sa2"] = leaf

    parent.request_cancel()
    assert leaf.cancel.is_set()


def test_one_sub_agent_blowing_up_does_not_spare_the_others():
    """Cancelling is the one operation that must not be all-or-nothing."""
    parent = _agent()
    bad = types.SimpleNamespace(request_cancel=lambda: (_ for _ in ()).throw(RuntimeError()))
    good = _agent()
    parent._active_subagents["bad"] = bad
    parent._active_subagents["good"] = good

    parent.request_cancel()
    assert good.cancel.is_set()


# ------------------------------------------------- shell commands --------

def test_cancelling_a_turn_kills_the_command_it_is_blocked_on():
    """The per-command Stop button has always been able to do this. The button
    that stops the TURN could not, so a turn stuck on a dev server ignored it."""
    ag = _agent()
    killed = []
    ag._current_call_token = "tok123"
    orig = tools.stop_foreground
    try:
        import glmcode.agent as agent_mod
        agent_mod.stop_foreground = lambda t: killed.append(t) or True
        ag.request_cancel()
    finally:
        import glmcode.agent as agent_mod
        agent_mod.stop_foreground = orig
    assert killed == ["tok123"]


def test_a_turn_that_is_not_in_a_tool_kills_nothing():
    ag = _agent()
    called = []
    import glmcode.agent as agent_mod
    orig = agent_mod.stop_foreground
    try:
        agent_mod.stop_foreground = lambda t: called.append(t) or True
        ag.request_cancel()
    finally:
        agent_mod.stop_foreground = orig
    assert called == []
    assert ag.cancel.is_set()


def test_the_token_is_readable_from_another_thread():
    """tools._call_token is thread-local ON PURPOSE, so parallel chats cannot
    see each other's -- which is exactly why the GUI thread calling
    request_cancel cannot read it, and why the agent keeps its own copy."""
    ag = _agent()
    ag._current_call_token = "tok456"
    seen = {}
    t = threading.Thread(target=lambda: seen.update(v=ag._current_call_token))
    t.start(); t.join(5)
    assert seen["v"] == "tok456"

    tools.set_call_token("thread-local-only")
    out = {}
    t2 = threading.Thread(target=lambda: out.update(v=tools.get_call_token()))
    t2.start(); t2.join(5)
    assert out["v"] is None, "the thread-local leaked; the copy would be pointless"


def test_the_token_is_cleared_when_the_tool_returns():
    """Otherwise a Stop pressed while the turn is THINKING would kill whatever
    process last ran under that token -- if one somehow still existed."""
    import inspect
    src = inspect.getsource(Agent._handle_tool_calls)
    assert "self._current_call_token = None" in src
    assert src.index("self._current_call_token = run_token") < \
        src.index("self._current_call_token = None")


# ------------------------------------------------- and the old one ------

def test_a_blocked_permission_card_is_still_released():
    ag = _agent()
    ev = threading.Event()
    ag._worker_perms["r1"] = {"event": ev, "answer": ("y", "")}
    ag.request_cancel()
    assert ev.is_set()
    assert ag._worker_perms["r1"]["answer"][0] == "n"
