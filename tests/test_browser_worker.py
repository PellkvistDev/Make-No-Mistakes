"""The browser agent as a background WORKER, not a blocking call.

Asked for: "the control chrome mode should be better. Now it's hard to interact
with when it's working... the main agent can't check on it or work in parallel.
And it would be cool if we could use this in voice mode, as a normal worker, so
that I can see the browser and talk to it and the main agent steers the browser
worker."

Both halves were missing. control_chrome ran the Browser Agent inline and the
whole conversation waited on it -- the one thing a user is most likely to be
WATCHING was the one thing they could not talk about while it happened. And the
coding agent had no worker tools at all, so even when something was running it
had no way to ask.
"""

import threading
import time
import types

import pytest

from glmcode import tools
from glmcode.agent import Agent, _browser_worker_name


def _agent(monkeypatch, report="Did the thing.", block=None):
    """A coordinator with the browser and sub-agent machinery faked out."""
    ag = Agent.__new__(Agent)
    ag.allow_subagents = True
    ag.backup_repo = None
    ag.browser_session = object()
    ag._workers, ag._workers_lock = {}, threading.Lock()
    ag._worker_seq = 0
    ag._worker_limiter = None
    ag._emit_lock = threading.Lock()
    ag._active_subagents, ag._active_subagents_lock = {}, threading.Lock()
    ag.session_usage = types.SimpleNamespace(add=lambda u: None)
    ag.events = types.SimpleNamespace(
        worker_update=lambda *a, **k: None,
        subagent=lambda *a, **k: None,
        info=lambda m: None, warn=lambda m: None)
    ag._emit_subagent = lambda *a, **k: None
    ag._worker_changed_files = lambda wid: []
    ag._describe_changes = lambda c: ""
    seen = {}

    def browser(goal, session, aid):
        seen["goal"] = goal
        seen["aid"] = aid
        if block is not None:
            block.wait(5)
        return report

    ag._run_browser_subagent = browser
    ag._ensure_browser_session = lambda: ag.browser_session
    ag.seen = seen
    return ag


# --------------------------------------------------------------------- #

def test_control_chrome_still_blocks_by_default():
    """The simple case -- a quick look where the answer is needed to carry on --
    is unchanged, and so is every existing caller."""
    ag = _agent(None, report="The page says 42.")
    out = ag._control_chrome_tool("read the page")
    assert "42" in out
    assert ag._workers == {}, "a blocking call should not create a worker"


def test_background_returns_at_once_with_a_worker_to_ask_about():
    gate = threading.Event()
    ag = _agent(None, block=gate)
    out = ag._control_chrome_tool("open my dashboard", background=True)
    try:
        assert "wk1" in out
        assert len(ag._workers) == 1
        assert ag._workers["wk1"]["kind"] == "browser"
        assert ag._workers["wk1"]["status"] == "running"
        # and it is genuinely still going, not finished-and-reported
        assert "steer_worker" in out
    finally:
        gate.set()


def test_the_worker_reports_through_the_same_registry(monkeypatch):
    ag = _agent(None, report="Found 3 errors.")
    ag._control_chrome_tool("check the errors", background=True)
    for _ in range(100):
        if ag._workers["wk1"]["status"] != "running":
            break
        time.sleep(0.02)
    assert ag._workers["wk1"]["status"] == "done"
    assert ag._workers["wk1"]["result"] == "Found 3 errors."
    assert "Found 3 errors." in ag._check_workers()


def test_a_start_url_reaches_the_background_worker_too():
    gate = threading.Event()
    ag = _agent(None, block=gate)
    ag._control_chrome_tool("buy milk", start_url="shop.example", background=True)
    try:
        for _ in range(100):
            if ag.seen.get("goal"):
                break
            time.sleep(0.02)
        assert "shop.example" in ag.seen["goal"]
    finally:
        gate.set()


def test_a_browser_that_will_not_start_is_reported_to_the_caller():
    """Opened before the thread is started, so "the browser would not open" is
    an answer the model can act on rather than a report nobody is waiting for."""
    ag = _agent(None)

    def boom():
        raise RuntimeError("no browser here")
    ag._ensure_browser_session = boom
    with pytest.raises(RuntimeError):
        ag._control_chrome_tool("anything", background=True)
    assert ag._workers == {}, "a worker was registered for a browser that never opened"


def test_it_can_be_steered_and_stopped_like_any_other_worker():
    gate = threading.Event()
    ag = _agent(None, block=gate)
    ag._control_chrome_tool("do a long thing", background=True)
    try:
        # by id and by the name derived from the goal
        assert ag._resolve_worker("wk1") == "wk1"
        assert ag._resolve_worker("do-long-thing") == "wk1"
    finally:
        gate.set()


def test_workers_get_a_name_worth_saying_out_loud():
    """check_workers and steer_worker take a name as well as an id, and "wk3"
    is not something anyone says -- least of all in voice mode, where these are
    most useful."""
    assert _browser_worker_name("Open my dashboard and check the error count") \
        == "open-dashboard-check-error"
    assert _browser_worker_name("") == "browser"


# --------------------------------------------------------------------- #
# The two tool surfaces

def test_the_coding_agent_can_now_ask_about_what_it_started():
    """It had NO worker tools. Everything it could delegate blocked until it
    finished, so there was never anything running to ask about -- which is
    exactly what made a background browser impossible."""
    names = [s["function"]["name"] for s in tools.WORKER_SCHEMAS]
    assert names == ["check_workers", "steer_worker", "stop_worker"]


def test_control_chrome_offers_the_background_mode():
    schema = next(s for s in tools.TOOL_SCHEMAS
                  if s["function"]["name"] == "control_chrome")
    props = schema["function"]["parameters"]["properties"]
    assert "background" in props
    assert "background=true" in schema["function"]["description"]


def test_voice_can_dispatch_a_browser_worker():
    """"I can see the browser and talk to it and the main agent steers the
    browser worker" -- the spoken side needs the same thing."""
    schema = next(s for s in tools.CONVERSATIONAL_SCHEMAS
                  if s["function"]["name"] == "dispatch_worker")
    kind = schema["function"]["parameters"]["properties"]["kind"]
    assert kind["enum"] == ["code", "browser"]
    assert "browser" in kind["description"]


def test_the_kind_reaches_the_dispatcher():
    ag = _agent(None)
    started = {}
    ag._dispatch_worker = lambda name, task, kind="code": started.update(
        name=name, task=task, kind=kind) or "ok"
    ag._run_tool("dispatch_worker", {"name": "n", "task": "t", "kind": "browser"})
    assert started["kind"] == "browser"


def test_both_prompts_say_when_to_use_it():
    """A tool the model never reaches for is not a feature."""
    from glmcode.prompts import SYSTEM_PROMPT, CONVERSATIONAL_SYSTEM
    assert "background=true" in SYSTEM_PROMPT
    assert "check_workers" in SYSTEM_PROMPT
    # the spoken side: browsing is a worker, and it must not try to do it itself
    assert 'kind="browser"' in CONVERSATIONAL_SYSTEM
    assert "steer_worker it when they change their mind" in CONVERSATIONAL_SYSTEM
