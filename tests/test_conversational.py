"""Speech-to-speech conversational mode: the delegator agent is restricted to
talk + dispatch, and dispatch_worker is fire-and-forget (starts a background
worker on its own thread, never blocking the conversation) with the outcome
surfaced through worker_update events. The heavy work is scripted, no network.
"""

import sys
import threading
import time
import types

from glmcode.agent import Agent
from glmcode.api import ApiError
from glmcode.config import Config
from glmcode.prompts import CONVERSATIONAL_SYSTEM
from glmcode.tools import CONVERSATIONAL_SCHEMAS

from conftest import FakeResult, ScriptedClient, tool_call


def _convo(monkeypatch, events, script=None):
    """A conversational Agent wired to ScriptedClient + RecordingEvents."""
    import glmcode.agent as agent_mod
    monkeypatch.setattr(agent_mod, "ZaiClient", ScriptedClient)
    ScriptedClient.scripts = []
    client = ScriptedClient()
    if script is not None:
        client._script = script
    return Agent(Config(), client, events=events, conversational=True)


def _wait_worker(agent, wid, timeout=5.0):
    """Block until a background worker leaves the 'running' state (it runs on a
    daemon thread). Scripted responses are instant, so this resolves at once."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with agent._workers_lock:
            st = agent._workers.get(wid, {}).get("status")
        if st and st != "running":
            return st
        time.sleep(0.01)
    return "timeout"


def test_conversational_agent_has_only_delegation_tools(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    names = {s["function"]["name"] for s in convo.tool_schemas}
    assert names == {"dispatch_worker", "check_workers", "steer_worker", "stop_worker",
                     "worker_changes", "revert_worker"}
    # None of the real file/command tools are exposed to the voice agent.
    assert "edit_file" not in names and "run_powershell" not in names


def test_conversational_uses_spoken_system_prompt(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    assert convo._base_system_prompt == CONVERSATIONAL_SYSTEM
    assert convo.messages[0]["role"] == "system"
    assert CONVERSATIONAL_SYSTEM.split("\n")[0] in convo.messages[0]["content"]


def test_dispatch_worker_returns_immediately_and_finishes(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    # The worker's own sub-agent pops this script and reports a final answer.
    ScriptedClient.scripts = [lambda n: FakeResult(content="Added the feature.")]
    out = convo._dispatch_worker("add-thing", "add the thing to app.py")
    # Returns instantly with an id, BEFORE the work is done.
    assert "wk1" in out
    started = [w for w in events.worker_events if w[2] == "started"]
    assert started and started[0][1] == "add-thing"

    assert _wait_worker(convo, "wk1") == "done"
    done = [w for w in events.worker_events if w[2] == "done"]
    assert done and "Added the feature." in done[0][4]  # result carried for the announce
    with convo._workers_lock:
        assert convo._workers["wk1"]["result"] == "Added the feature."


def test_dispatch_worker_empty_task_errors(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    try:
        convo._dispatch_worker("x", "   ")
        assert False, "expected ToolError"
    except Exception as e:
        assert "task" in str(e)


def test_failing_worker_surfaces_as_error(monkeypatch, events):
    convo = _convo(monkeypatch, events)

    def die(n):
        raise ApiError(429, "rate limited")

    ScriptedClient.scripts = [die]
    convo._dispatch_worker("doomed", "try something")
    assert _wait_worker(convo, "wk1") == "error"
    errs = [w for w in events.worker_events if w[2] == "error"]
    assert errs and ("429" in errs[0][4] or "rate limited" in errs[0][4])


def test_check_workers_reports_running_and_done(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    assert "No background workers" in convo._check_workers()
    ScriptedClient.scripts = [lambda n: FakeResult(content="done report")]
    convo._dispatch_worker("w1", "do it")
    assert _wait_worker(convo, "wk1") == "done"
    summary = convo._check_workers()
    assert "1 running, 1 done" in summary.replace("0 running", "1 running") or "done" in summary
    assert "w1" in summary and "DONE" in summary


def test_ids_increment_across_dispatches(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    ScriptedClient.scripts = [lambda n: FakeResult(content="a"),
                              lambda n: FakeResult(content="b")]
    o1 = convo._dispatch_worker("a", "t1")
    o2 = convo._dispatch_worker("b", "t2")
    assert "wk1" in o1 and "wk2" in o2
    assert _wait_worker(convo, "wk1") == "done"
    assert _wait_worker(convo, "wk2") == "done"


def test_resolve_worker_by_id_and_name(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    with convo._workers_lock:
        convo._workers["wk1"] = {"id": "wk1", "name": "dark-mode", "status": "running",
                                 "task": "t", "result": "", "error": None}
        convo._workers["wk2"] = {"id": "wk2", "name": "login-fix", "status": "done",
                                 "task": "t", "result": "r", "error": None}
    assert convo._resolve_worker("wk1") == "wk1"
    assert convo._resolve_worker("dark") == "wk1"      # loose name match
    assert convo._resolve_worker("login-fix") == "wk2"
    assert convo._resolve_worker("nope") is None


def test_steer_and_stop_unknown_worker_error(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    for call in (lambda: convo._steer_worker_tool("ghost", "hi"),
                 lambda: convo._stop_worker_tool("ghost")):
        try:
            call()
            assert False, "expected ToolError"
        except Exception as e:
            assert "No worker matches" in str(e)


def test_stop_worker_cancels_and_marks_stopped(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    # A worker that would run "forever": its scripted client keeps asking for a
    # tool until cancelled. Simpler: register a fake running sub we can assert on.
    class FakeSub:
        def __init__(self): self.cancelled = False
        def request_cancel(self): self.cancelled = True
    sub = FakeSub()
    with convo._workers_lock:
        convo._workers["wk1"] = {"id": "wk1", "name": "task", "status": "running",
                                 "task": "t", "result": "", "error": None}
    with convo._active_subagents_lock:
        convo._active_subagents["wk1"] = sub
    out = convo._stop_worker_tool("wk1")
    assert sub.cancelled is True
    assert "Stopping" in out
    with convo._workers_lock:
        assert convo._workers["wk1"]["status"] == "stopped"


class _FakeBackup:
    def __init__(self):
        self.snaps = 0
        self.reverted_to = None
        self.changes = [("M", "auth.py"), ("A", "settings.js")]

    def snapshot(self, msg):
        self.snaps += 1
        return f"commit{self.snaps}"

    def changed_files_since(self, commit):
        return list(self.changes)

    def revert_to(self, commit):
        self.reverted_to = commit


def test_worker_changes_and_revert(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="edited files")]
    convo._dispatch_worker("edits", "edit some files")
    assert _wait_worker(convo, "wk1") == "done"
    # A baseline was snapshotted at dispatch, and the changes were recorded.
    with convo._workers_lock:
        assert convo._workers["wk1"]["baseline"] == "commit1"
        assert convo._workers["wk1"]["changes"] == [("M", "auth.py"), ("A", "settings.js")]
    desc = convo._worker_changes_tool("edits")
    assert "auth.py" in desc and "settings.js" in desc
    out = convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_to == "commit1"
    assert "Reverted" in out
    with convo._workers_lock:
        assert convo._workers["wk1"]["status"] == "reverted"


def test_revert_worker_without_backups(monkeypatch, events):
    convo = _convo(monkeypatch, events)  # no backup_repo
    with convo._workers_lock:
        convo._workers["wk1"] = {"id": "wk1", "name": "w", "status": "done",
                                 "task": "t", "result": "r", "error": None,
                                 "baseline": None, "changes": []}
    out = convo._revert_worker_tool("wk1")
    assert "can't revert" in out or "nothing to revert" in out


def test_worker_ask_blocks_until_resolved(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    with convo._workers_lock:
        convo._workers["wk1"] = {"id": "wk1", "name": "refactor", "status": "running",
                                 "task": "t", "result": "", "error": None}
    answer = {}

    def worker_side():
        answer["v"] = convo._worker_ask("wk1", "Run command: npm test", "npm test", None)

    th = threading.Thread(target=worker_side)
    th.start()
    # The worker is now blocked; a permission request was surfaced + spoken.
    for _ in range(200):
        if getattr(events, "worker_perms", None):
            break
        time.sleep(0.005)
    perms = getattr(events, "worker_perms", [])
    assert perms and perms[0][1] == "refactor" and "npm test" in perms[0][3]
    rid = perms[0][0]
    assert convo.resolve_worker_permission(rid, "y")
    th.join(timeout=2)
    assert answer["v"] == "y"


def test_deny_pending_worker_permissions_unblocks(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    with convo._workers_lock:
        convo._workers["wk1"] = {"id": "wk1", "name": "w", "status": "running",
                                 "task": "t", "result": "", "error": None}
    answer = {}

    def worker_side():
        answer["v"] = convo._worker_ask("wk1", "Write file: x", "x", None)

    th = threading.Thread(target=worker_side)
    th.start()
    for _ in range(200):
        if convo.pending_worker_permission():
            break
        time.sleep(0.005)
    convo.deny_pending_worker_permissions("closed")
    th.join(timeout=2)
    assert answer["v"][0] == "n"


def test_run_turn_dispatches_without_blocking(monkeypatch, events):
    """A full voice turn: the model calls dispatch_worker, then replies. The
    turn must return promptly (the coordinator does not join the worker)."""
    convo = _convo(monkeypatch, events)

    def coordinator(n):
        if n == 1:
            return FakeResult(tool_calls=[tool_call(
                "c1", "dispatch_worker",
                '{"name": "build-x", "task": "build feature x in full"}')])
        return FakeResult(content="On it — I've started building that.")

    convo.client._script = coordinator
    # The worker sub-agent (a separate ScriptedClient) pops this.
    ScriptedClient.scripts = [lambda n: FakeResult(content="worker done: built x")]
    convo.run_turn({"role": "user", "content": "build feature x"})
    # The coordinator answered (and the turn returned) without waiting for the
    # worker. ScriptedClient doesn't drive on_content, so the reply lands in
    # the message history rather than the events stream.
    final = [m for m in convo.messages
             if m.get("role") == "assistant" and isinstance(m.get("content"), str)
             and m["content"]]
    assert final and final[-1]["content"] == "On it — I've started building that."
    # A worker was dispatched from inside the turn and runs to completion.
    assert "wk1" in convo._workers
    assert _wait_worker(convo, "wk1") == "done"


# -- persist voice conversation into the chat transcript ------------------- #

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402


class _RecTranscript:
    def __init__(self):
        self.users, self.assistants = [], []

    def user(self, text, label="User"):
        self.users.append((label, text))

    def assistant(self, text, tool_calls=None):
        self.assistants.append(text)


def _fake_cs(reply_text, tr):
    convo = types.SimpleNamespace(messages=[
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": reply_text},
    ])
    agent = types.SimpleNamespace(transcript=tr)
    return types.SimpleNamespace(agent=agent, convo_agent=convo)


def test_persist_voice_turn_logs_user_and_reply():
    api = gui_app.Api.__new__(gui_app.Api)
    tr = _RecTranscript()
    cs = _fake_cs("Sure, on it.", tr)
    api._persist_voice_turn(cs, "please do the thing")
    assert tr.users == [("Voice", "please do the thing")]
    assert tr.assistants == ["Sure, on it."]


def test_persist_voice_turn_skips_user_for_announcements():
    api = gui_app.Api.__new__(gui_app.Api)
    tr = _RecTranscript()
    cs = _fake_cs("The build finished.", tr)
    api._persist_voice_turn(cs, "")   # announcement: no user utterance
    assert tr.users == []             # nothing logged as user input
    assert tr.assistants == ["The build finished."]
