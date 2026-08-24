"""A finished sub-agent can be picked back up instead of replaced.

Asked for: "the ability to continue subagents after they finish -- the main
agent should be able to use some resume tool on them with more prompt".

The gap was real and it was structural. `_run_single_subagent` popped the sub
out of `_active_subagents` in a `finally` and returned only its report text, so
the moment it finished the Agent -- its whole conversation, every file it had
read, everything it had worked out -- was dropped. A follow-up meant
`spawn_agents` again with a fresh sub-agent that had to be told all of that
background a second time, in a prompt the coordinator had to reconstruct.

Resuming is the same tool call as steering, one state later: `steer_worker`
while it runs, `resume_agent` once it has stopped.
"""

import threading
import time
import types

import pytest

from glmcode.agent import Agent, MAX_RESUMABLE
from glmcode.prompts import RESUME_PREAMBLE, SUBAGENT_PREAMBLE
from glmcode.tools import ToolError


class _Sub:
    """A sub-agent that records the turns it is given and answers each one."""

    def __init__(self, reply="did the thing"):
        self.messages = []
        self.turns = []
        self.session_usage = types.SimpleNamespace(add=lambda u: None)
        self._reply = reply

    def run_turn(self, msg):
        self.turns.append(msg["content"])
        self.messages.append(msg)
        self.messages.append({"role": "assistant", "content": self._reply})


def _agent():
    ag = Agent.__new__(Agent)
    ag.allow_subagents = True
    ag.conversational = False
    ag.backup_repo = None
    ag._workers, ag._workers_lock = {}, threading.Lock()
    ag._worker_seq = 0
    ag._worker_limiter = None
    ag._worker_perms, ag._worker_perms_lock = {}, threading.Lock()
    ag._emit_lock = threading.Lock()
    ag._active_subagents, ag._active_subagents_lock = {}, threading.Lock()
    from collections import OrderedDict
    ag._finished_agents, ag._finished_lock = OrderedDict(), threading.Lock()
    ag.session_usage = types.SimpleNamespace(add=lambda u: None)
    ag.events = types.SimpleNamespace(worker_update=lambda *a, **k: None,
                                      subagent=lambda *a, **k: None)
    ag._emit_subagent = lambda *a, **k: None
    ag._worker_changed_files = lambda wid: []
    ag._describe_changes = lambda c: ""
    return ag


def _finish(ag, aid="sa1", name="research", reply="did the thing"):
    """Run one sub-agent to completion through the real turn/report path."""
    sub = _Sub(reply)
    sink = types.SimpleNamespace(text="", last_error="")
    with ag._active_subagents_lock:
        ag._active_subagents[aid] = sub
    ag._subagent_turn(sub, sink, aid, name,
                      SUBAGENT_PREAMBLE.format(name=name, task="do the thing"))
    return sub


# ------------------------------------------------- it is kept at all ------

def test_a_finished_subagent_is_still_reachable():
    ag = _agent()
    _finish(ag)
    assert ag._resolve_finished("research") == "sa1"


def test_it_is_no_longer_counted_as_running():
    """Kept for resume is not the same as still going -- steer and stop must
    not think there is a live turn to reach."""
    ag = _agent()
    _finish(ag)
    assert ag._active_subagents == {}


def test_it_answers_to_its_id_as_well_as_its_name():
    ag = _agent()
    _finish(ag, aid="wk3", name="dark-mode")
    assert ag._resolve_finished("wk3") == "wk3"


def test_a_name_inside_a_sentence_finds_it():
    """"carry on with the dark-mode one" is how this gets asked for out loud."""
    ag = _agent()
    _finish(ag, aid="wk3", name="dark-mode")
    assert ag._resolve_finished("the dark-mode one") == "wk3"


def test_the_most_recent_wins_a_tie():
    ag = _agent()
    _finish(ag, aid="sa1", name="tests")
    _finish(ag, aid="sa2", name="tests")
    assert ag._resolve_finished("tests") == "sa2"


# ------------------------------------------------- resuming it -----------

def test_it_keeps_its_own_history_rather_than_starting_over():
    """The whole point. The second turn is appended to the first one's
    conversation, so everything it worked out is still in front of it."""
    ag = _agent()
    sub = _finish(ag)
    before = len(sub.messages)
    ag._resume_agent_tool("research", "now write the tests for it")
    assert len(sub.messages) > before
    assert sub.turns[0].startswith("You are \"research\"")
    assert "now write the tests for it" in sub.turns[1]


def test_the_follow_up_is_framed_as_more_work_not_a_summary():
    """Same lesson as the steer nudge: this arrives after a final report, which
    is the one place the model has just been told to stop."""
    ag = _agent()
    sub = _finish(ag)
    ag._resume_agent_tool("research", "add the dark theme too")
    resumed = sub.turns[1]
    assert resumed == RESUME_PREAMBLE.format(task="add the dark theme too")
    assert resumed.rstrip().endswith("add the dark theme too")


def test_the_report_says_it_was_resumed():
    ag = _agent()
    _finish(ag, reply="wrote the tests")
    out = ag._resume_agent_tool("research", "write the tests")
    assert "resumed" in out
    assert "wrote the tests" in out


def test_it_can_be_resumed_more_than_once():
    ag = _agent()
    sub = _finish(ag)
    ag._resume_agent_tool("research", "step two")
    ag._resume_agent_tool("research", "step three")
    assert len(sub.turns) == 3


# ------------------------------------------------- refusing honestly -----

def test_a_running_worker_is_sent_to_steer_worker():
    """Not "no agent matches": the model would spawn a duplicate of something
    already doing the work."""
    ag = _agent()
    ag._workers["wk1"] = {"id": "wk1", "name": "dark-mode", "status": "running"}
    with pytest.raises(ToolError) as e:
        ag._resume_agent_tool("dark-mode", "also do X")
    assert "steer_worker" in str(e.value)


def test_an_unknown_one_says_what_to_do_instead():
    ag = _agent()
    with pytest.raises(ToolError) as e:
        ag._resume_agent_tool("nothing-like-this", "carry on")
    assert "spawn a fresh one" in str(e.value)


def test_an_empty_task_is_refused():
    ag = _agent()
    _finish(ag)
    with pytest.raises(ToolError):
        ag._resume_agent_tool("research", "   ")


def test_only_the_last_few_stay_resumable():
    """Each is holding its whole conversation. A long chat that spawned thirty
    would otherwise keep thirty histories alive for the session."""
    ag = _agent()
    for n in range(MAX_RESUMABLE + 3):
        _finish(ag, aid=f"sa{n}", name=f"agent-{n}")
    assert len(ag._finished_agents) == MAX_RESUMABLE
    assert ag._resolve_finished("agent-0") is None
    assert ag._resolve_finished(f"agent-{MAX_RESUMABLE + 2}") is not None


def test_a_dropped_one_says_so_rather_than_starting_from_nothing():
    """Silently spawning a blank sub-agent under a name the coordinator thinks
    it is continuing is the worst version of this."""
    ag = _agent()
    for n in range(MAX_RESUMABLE + 1):
        _finish(ag, aid=f"sa{n}", name=f"agent-{n}")
    with pytest.raises(ToolError) as e:
        ag._resume_agent_tool("agent-0", "carry on")
    assert str(MAX_RESUMABLE) in str(e.value)


# ------------------------------------------------- a resumed WORKER ------

def test_resuming_a_worker_puts_its_record_back_to_running_and_out_again():
    """A worker's id IS its sub-agent id, so one registry serves both -- but a
    worker also has a record that check_workers reads, and it has to end up in
    the same shape as one that ran once."""
    ag = _agent()
    seen = []
    ag.events = types.SimpleNamespace(
        worker_update=lambda wid, name, status, summary="", result="":
            seen.append(status),
        subagent=lambda *a, **k: None)
    ag._workers["wk1"] = {"id": "wk1", "name": "dark-mode", "status": "done",
                          "result": "first pass", "error": None, "changes": [],
                          "wrote": [], "baseline": None, "kind": "code",
                          "task": "t"}
    _finish(ag, aid="wk1", name="dark-mode", reply="second pass")
    out = ag._resume_agent_tool("wk1", "now the tests")
    assert "second pass" in out
    assert seen == ["started", "done"]
    assert ag._workers["wk1"]["status"] == "done"
    assert ag._workers["wk1"]["result"] == "second pass"
