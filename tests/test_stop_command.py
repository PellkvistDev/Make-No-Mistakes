"""Stopping a blocking foreground command: a never-ending shell command
(a dev server, a watch) must not freeze the turn -- the UI's Stop button
kills its process tree so run_powershell returns and the agent continues."""

import subprocess
import threading

import pytest

import glmcode.tools as tools
from glmcode.tools import (get_call_token, run_powershell, set_call_token,
                           stop_foreground)

from conftest import FakeResult, tool_call


class FakePopen:
    """A powershell stand-in: communicate() blocks until the process is
    'killed' (terminate/kill) or the timeout fires -- exactly the shape
    run_powershell relies on, but with no real PowerShell (absent on Linux
    CI) and no real waiting for the stop path."""

    def __init__(self, *a, **k):
        self._done = threading.Event()
        self.returncode = None
        self.pid = 4321

    def communicate(self, timeout=None):
        fired = self._done.wait(timeout)
        if not fired:
            raise subprocess.TimeoutExpired(cmd="powershell", timeout=timeout)
        return (b"some partial output", b"")

    def terminate(self):
        self.returncode = -15
        self._done.set()

    def kill(self):
        self.returncode = -9
        self._done.set()

    def poll(self):
        return self.returncode


@pytest.fixture(autouse=True)
def clean_registry():
    # Never leak a token between tests.
    tools._foreground_procs.clear()
    tools._stopped_tokens.clear()
    set_call_token(None)
    yield
    tools._foreground_procs.clear()
    tools._stopped_tokens.clear()
    set_call_token(None)


def test_stop_foreground_kills_and_returns_stopped_message(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", FakePopen)
    # Patching subprocess.Popen module-wide means _terminate_process_tree's
    # Windows taskkill path (subprocess.run -> `with Popen(...)`) would re-enter
    # the FakePopen, which isn't a context manager. Stub the terminator to just
    # signal our fake process -- the test is about the stop flow, not taskkill.
    monkeypatch.setattr(tools, "_terminate_process_tree", lambda proc: proc.terminate())
    result = {}

    def run():
        set_call_token("tok-A")
        result["out"] = run_powershell("npm run dev", timeout_seconds=600)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Wait for the command to register as running, then stop it from "another
    # thread" (as the GUI would).
    for _ in range(200):
        if tools._foreground_procs.get("tok-A"):
            break
        threading.Event().wait(0.01)
    assert stop_foreground("tok-A") is True
    t.join(timeout=5)
    assert not t.is_alive()
    assert "Stopped by the user" in result["out"]
    assert "some partial output" in result["out"]
    # registry cleaned up
    assert "tok-A" not in tools._foreground_procs
    assert "tok-A" not in tools._stopped_tokens


def test_stop_foreground_unknown_token_is_noop():
    assert stop_foreground("nope") is False
    assert stop_foreground("") is False


def test_timeout_kills_tree_and_points_at_run_background(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(tools, "_terminate_process_tree", lambda proc: proc.terminate())
    set_call_token("tok-B")
    with pytest.raises(tools.ToolErrorBase) as ei:
        run_powershell("npm run dev", timeout_seconds=1)  # clamped min, ~1s
    msg = str(ei.value)
    assert "timed out" in msg and "run_background" in msg
    # even a timeout cleans the registry (no orphan token lingering)
    assert "tok-B" not in tools._foreground_procs


class InstantPopen(FakePopen):
    """Completes on its own the moment it's waited on -- a normal, fast
    command that returns before anyone would think to stop it."""

    def communicate(self, timeout=None):
        self.returncode = 0
        return (b"hi", b"")


def test_untokened_command_still_runs_and_registers_nothing(monkeypatch):
    """Internal callers (git helpers, run_tests) run without a token; they
    must still work, just without being stoppable."""
    monkeypatch.setattr(tools.subprocess, "Popen", InstantPopen)
    set_call_token(None)
    out = run_powershell("echo hi", timeout_seconds=600)
    assert "hi" in out and "[exit code: 0]" in out
    assert not tools._foreground_procs  # nothing to stop, nothing registered


def test_tokened_command_deregisters_on_normal_exit(monkeypatch):
    monkeypatch.setattr(tools.subprocess, "Popen", InstantPopen)
    set_call_token("tok-C")
    run_powershell("echo hi", timeout_seconds=600)
    # a later Stop click on a finished command finds nothing and no-ops
    assert stop_foreground("tok-C") is False
    assert "tok-C" not in tools._foreground_procs


# -- agent token plumbing ------------------------------------------------- #

class ToolCallRecorder:
    def __init__(self, inner):
        self.inner = inner
        self.tool_calls = []

    def __getattr__(self, k):
        return getattr(self.inner, k)

    def tool_call(self, name, args, call_id=""):
        self.tool_calls.append((name, call_id))
        self.inner.tool_call(name, args, call_id)


def test_agent_sets_and_clears_call_token(scripted_agent, monkeypatch):
    import glmcode.agent as agent_mod

    seen = {}

    def fake_execute(name, args):
        seen["token_during"] = agent_mod.get_call_token() if hasattr(
            agent_mod, "get_call_token") else tools.get_call_token()
        return "ok"

    monkeypatch.setattr(agent_mod, "execute_tool", fake_execute)

    calls = iter([
        FakeResult(tool_calls=[tool_call("c1", "run_powershell",
                                         '{"command": "npm run dev"}')]),
        FakeResult(content="done"),
    ])
    agent = scripted_agent(lambda n: next(calls))
    agent.set_mode("yolo")  # auto-approve so the tool actually dispatches
    rec = ToolCallRecorder(agent.events)
    agent.events = rec

    agent.run_turn({"role": "user", "content": "start the server"})

    # A token was live during the tool call...
    assert seen["token_during"]
    assert len(seen["token_during"]) == 12
    # ...it matches the one handed to the UI on the tool box...
    assert ("run_powershell", seen["token_during"]) in rec.tool_calls
    # ...and it's cleared once the turn is over (no leak onto the thread).
    assert tools.get_call_token() is None
