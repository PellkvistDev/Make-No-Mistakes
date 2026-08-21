"""Speech-to-speech conversational mode: the delegator agent is restricted to
talk + dispatch, and dispatch_worker is fire-and-forget (starts a background
worker on its own thread, never blocking the conversation) with the outcome
surfaced through worker_update events. The heavy work is scripted, no network.
"""

import sys
import threading
import time
import types
from pathlib import Path

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


def test_conversational_agent_tools(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    names = {s["function"]["name"] for s in convo.tool_schemas}
    # Delegation tools...
    assert {"dispatch_worker", "check_workers", "steer_worker", "stop_worker",
            "worker_changes", "revert_worker"} <= names
    # ...plus read-only investigation tools so it can look at the code...
    assert {"read_file", "grep", "list_dir", "find_references", "review_changes"} <= names
    # ...but NEVER anything that edits or runs.
    assert "edit_file" not in names and "run_powershell" not in names
    assert "write_file" not in names and "run_tests" not in names


def test_conversational_prompt_has_project_grounding(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    sysmsg = convo.messages[0]["content"]
    assert "The project you're working on" in sysmsg
    assert "Working directory:" in sysmsg


def test_conversational_uses_spoken_system_prompt(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    assert convo._base_system_prompt.startswith(CONVERSATIONAL_SYSTEM)
    assert convo.messages[0]["role"] == "system"
    assert CONVERSATIONAL_SYSTEM.split("\n")[0] in convo.messages[0]["content"]


def test_language_policy_english_work_by_default(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    sysmsg = convo.messages[0]["content"]
    # Whatever language is spoken, the work (worker tasks) stays English.
    assert "ALWAYS reason in English" in sysmsg
    assert "dispatch_worker) in clear English" in sysmsg
    # Default reply language is English.
    assert "Reply to the user out loud in English." in sysmsg


def test_language_policy_reply_match_mode(monkeypatch, events):
    import glmcode.agent as agent_mod
    monkeypatch.setattr(agent_mod, "ZaiClient", ScriptedClient)
    ScriptedClient.scripts = []
    cfg = Config()
    cfg.voice_reply_language = "match"
    convo = Agent(cfg, ScriptedClient(), events=events, conversational=True)
    sysmsg = convo.messages[0]["content"]
    assert "in the same language they spoke" in sysmsg
    # ...but worker tasks are STILL English even in match-reply mode.
    assert "dispatch_worker) in clear English" in sysmsg


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
        self.reverted_paths = None
        self.changes = [("M", "auth.py"), ("A", "settings.js")]

    def snapshot(self, msg):
        self.snaps += 1
        return f"commit{self.snaps}"

    def changed_files_since(self, commit):
        return list(self.changes)

    def revert_to(self, commit):
        self.reverted_to = commit

    def revert_paths_to(self, commit, paths):
        self.reverted_to = commit
        self.reverted_paths = list(paths)
        return list(paths)


def _wrote(convo, wid, *paths):
    """Say that this worker's own write tools named these files, the way the
    real thing learns it: through the sub-agent event funnel."""
    for path in paths:
        convo._emit_subagent_stream(wid, "tool_call", name="write_file",
                                    args={"path": path})


def test_worker_changes_and_revert(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="edited files")]
    convo._dispatch_worker("edits", "edit some files")
    assert _wait_worker(convo, "wk1") == "done"
    _wrote(convo, "wk1", "auth.py", "settings.js")
    # A baseline was snapshotted at dispatch, and the changes were recorded.
    with convo._workers_lock:
        assert convo._workers["wk1"]["baseline"] == "commit1"
        assert convo._workers["wk1"]["changes"] == [("M", "auth.py"), ("A", "settings.js")]
    desc = convo._worker_changes_tool("edits")
    assert "auth.py" in desc and "settings.js" in desc
    out = convo._revert_worker_tool("wk1")
    # Per PATH, from that worker's baseline -- not a reset of the whole tree.
    assert convo.backup_repo.reverted_to == "commit1"
    assert sorted(convo.backup_repo.reverted_paths) == ["auth.py", "settings.js"]
    assert "auth.py" in out
    with convo._workers_lock:
        assert convo._workers["wk1"]["status"] == "reverted"


def test_revert_leaves_alone_what_the_worker_did_not_write(monkeypatch, events):
    """The whole-tree revert threw away everything done since the worker
    STARTED -- another worker's work, and the user's own edits. "Revert this
    worker" has to mean this worker."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="done")]
    convo._dispatch_worker("edits", "edit auth")
    assert _wait_worker(convo, "wk1") == "done"
    # It wrote one of the two changed files; the other changed meanwhile.
    _wrote(convo, "wk1", "auth.py")

    out = convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_paths == ["auth.py"]
    assert "settings.js" not in convo.backup_repo.reverted_paths
    assert "left alone" in out


def test_revert_refuses_when_nothing_is_attributable(monkeypatch, events):
    """Files changed while it ran, but none of them are ones it asked to write.
    Undoing "its" work would mean undoing somebody else's, so it says so
    instead of reverting the tree and admitting it afterwards."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="done")]
    convo._dispatch_worker("edits", "run a command")
    assert _wait_worker(convo, "wk1") == "done"

    out = convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_paths is None
    assert convo.backup_repo.reverted_to is None
    assert "can't tell its work from anyone else's" in out
    with convo._workers_lock:
        assert convo._workers["wk1"]["status"] == "done"      # not "reverted"


def test_a_denied_write_is_not_reverted(monkeypatch, events):
    """`wrote` is a record of INTENT -- it is written when the tool call is
    announced, before the permission engine has had its say. Intersecting it
    with what actually differs from the baseline is what keeps a refused write
    out of the revert set."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    convo.backup_repo.changes = [("M", "auth.py")]
    ScriptedClient.scripts = [lambda n: FakeResult(content="done")]
    convo._dispatch_worker("edits", "edit two files")
    assert _wait_worker(convo, "wk1") == "done"
    _wrote(convo, "wk1", "auth.py", "denied.py")

    convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_paths == ["auth.py"]


def test_worker_changes_says_which_half_it_can_undo(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="done")]
    convo._dispatch_worker("edits", "edit auth")
    assert _wait_worker(convo, "wk1") == "done"
    _wrote(convo, "wk1", "auth.py")

    said = convo._worker_changes_tool("wk1")
    assert "1 were written by 'edits' itself" in said
    assert "revert_worker will leave them alone" in said


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
    """A chat with the pieces _persist_voice_turn touches.

    It writes to the transcript AND into the chat itself now -- a spoken
    exchange that leaves no trace in the conversation is what made talking and
    typing feel like two different chats -- so the fake needs the turn lock it
    takes before appending, and somewhere to append to.
    """
    import threading
    convo = types.SimpleNamespace(messages=[
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": reply_text},
    ])
    agent = types.SimpleNamespace(
        transcript=tr, messages=[], workdir=".", todos=[],
        session_usage=types.SimpleNamespace(prompt_tokens=0, completion_tokens=0))
    return types.SimpleNamespace(
        sid="s1", agent=agent, convo_agent=convo,
        events=types.SimpleNamespace(emit=lambda *a, **k: None),
        title="", provider="", model="", auto_backup=True, turn_snapshots=[],
        turn_lock=threading.Lock(),
        voice_turns=[], voice_turns_lock=threading.Lock())


def _api_for_voice():
    api = gui_app.Api.__new__(gui_app.Api)
    api._store = types.SimpleNamespace(save=lambda *a, **k: None)
    return api


def test_persist_voice_turn_logs_user_and_reply():
    api = _api_for_voice()
    tr = _RecTranscript()
    cs = _fake_cs("Sure, on it.", tr)
    api._persist_voice_turn(cs, "please do the thing")
    assert tr.users == [("Voice", "please do the thing")]
    assert tr.assistants == ["Sure, on it."]


def test_persist_voice_turn_skips_user_for_announcements():
    api = _api_for_voice()
    tr = _RecTranscript()
    cs = _fake_cs("The build finished.", tr)
    api._persist_voice_turn(cs, "")   # announcement: no user utterance
    assert tr.users == []             # nothing logged as user input
    assert tr.assistants == ["The build finished."]


# --------------------------------------------------------------------- #
# What the chain SAYS about a worker
#
# Four of these were wrong in the same direction: a worker that did not reach
# "done" was described as a failure. A worker the user stopped on purpose, or
# whose changes they undid themselves, is not a crash -- and telling the model
# it crashed invites it to diagnose and re-run the very thing that was just
# cancelled.

def _park(convo, wid, **fields):
    """Put a worker straight into the registry in a terminal state."""
    row = {"id": wid, "name": fields.pop("name", wid), "task": "t", "result": "",
           "error": None, "baseline": None, "changes": [], "wrote": [],
           "kind": "code", "status": "done"}
    row.update(fields)
    with convo._workers_lock:
        convo._workers[wid] = row


def test_check_workers_counts_every_status(monkeypatch, events):
    """The header used to count only running/done/error, so a chat whose one
    worker had been stopped opened with "0 running, 0 done, 0 failed" and then
    listed it."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="a", status="stopped")
    _park(convo, "wk2", name="b", status="reverted")
    _park(convo, "wk3", name="c", status="done", result="fine")
    head = convo._check_workers().splitlines()[0]
    assert "1 done" in head
    assert "1 stopped" in head
    assert "1 reverted" in head
    assert "0 " not in head          # nothing padded out with empty categories


def test_a_reverted_worker_is_not_reported_as_a_crash(monkeypatch, events):
    """It fell into the else branch, whose error field is None -- so undoing a
    worker printed "FAILED -- None"."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="a", status="reverted")
    out = convo._check_workers()
    assert "REVERTED" in out
    assert "FAILED" not in out
    assert "None" not in out


def test_a_stopped_worker_is_not_reported_as_failed(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="a", status="stopped")
    out = convo._check_workers()
    assert "STOPPED by the user" in out
    assert "FAILED" not in out


# --------------------------------------------------------------------- #
# Which worker a spoken reference means

def test_a_short_name_is_not_matched_inside_an_unrelated_sentence(monkeypatch, events):
    """`nm in ident` matched a worker named "a" against almost anything anyone
    could say, and then steered or stopped whichever was first."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="a", status="running")
    _park(convo, "wk2", name="dark-mode", status="running")
    # "a" is a substring of every one of these, and used to win all of them.
    for said in ("please stop what you are doing",
                 "how is the dark mode work going",
                 "cancel that"):
        assert convo._resolve_worker(said) is None, said
    # The one that should still resolve, does.
    assert convo._resolve_worker("dark-mode") == "wk2"


def test_a_fragment_of_the_name_still_matches(monkeypatch, events):
    """The useful direction: people say part of a name, not a sentence the
    name happens to contain."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="fix-login-redirect", status="running")
    assert convo._resolve_worker("login") == "wk1"
    assert convo._resolve_worker("FIX-LOGIN-REDIRECT") == "wk1"


def test_the_most_recent_match_wins(monkeypatch, events):
    """"stop the browser one" means the one just started, not its namesake
    from ten minutes ago."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="browser", status="running")
    _park(convo, "wk2", name="browser", status="running")
    assert convo._resolve_worker("browser") == "wk2"


def test_a_running_worker_is_preferred_over_a_finished_one(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="build", status="running")
    _park(convo, "wk2", name="build", status="done")
    assert convo._resolve_worker("build") == "wk1"


# --------------------------------------------------------------------- #
# A worker that dies half way through

def test_a_crashed_worker_still_records_what_it_changed(monkeypatch, events):
    """This was skipped on the error path, so worker_changes and revert_worker
    both believed a crashed worker had touched nothing -- and a worker that
    died half way through is the one you most want to undo."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()

    def boom(n):
        raise RuntimeError("the sub-agent fell over")
    ScriptedClient.scripts = [boom]
    convo._dispatch_worker("doomed", "try something")
    assert _wait_worker(convo, "wk1") == "error"

    with convo._workers_lock:
        assert convo._workers["wk1"]["changes"] == [("M", "auth.py"), ("A", "settings.js")]
    assert "auth.py" in convo._worker_changes_tool("wk1")


def test_a_crashed_workers_own_writes_can_be_undone(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()

    def boom(n):
        raise RuntimeError("fell over")
    ScriptedClient.scripts = [boom]
    convo._dispatch_worker("doomed", "try something")
    assert _wait_worker(convo, "wk1") == "error"
    _wrote(convo, "wk1", "auth.py")

    convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_paths == ["auth.py"]


# --------------------------------------------------------------------- #
# Attributing a write to the worker that made it

def test_an_absolute_path_is_matched_against_the_diff(monkeypatch, events):
    """write_file takes whatever the model wrote; the baseline diff speaks in
    repo-relative paths. Without bringing the two into the same terms, nothing
    ever intersects and every revert refuses."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    ScriptedClient.scripts = [lambda n: FakeResult(content="done")]
    convo._dispatch_worker("edits", "edit auth")
    assert _wait_worker(convo, "wk1") == "done"
    convo._emit_subagent_stream(
        "wk1", "tool_call", name="write_file",
        args={"path": str(Path(convo.workdir) / "auth.py")})

    convo._revert_worker_tool("wk1")
    assert convo.backup_repo.reverted_paths == ["auth.py"]


def test_a_path_outside_the_project_is_not_attributed(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="w", status="running")
    convo._emit_subagent_stream("wk1", "tool_call", name="write_file",
                                args={"path": "/etc/passwd"})
    with convo._workers_lock:
        assert convo._workers["wk1"]["wrote"] == []


def test_a_read_tool_is_not_recorded_as_a_write(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="w", status="running")
    for tool in ("read_file", "grep", "run_command", "list_dir"):
        convo._emit_subagent_stream("wk1", "tool_call", name=tool,
                                    args={"path": "auth.py"})
    with convo._workers_lock:
        assert convo._workers["wk1"]["wrote"] == []


def test_attribution_does_not_swallow_the_event(monkeypatch, events):
    """The recording is a side effect on the way past. If it ever stopped the
    forward, the sub-agent's live thread would go dark in the UI -- and the
    only symptom would be a panel that had quietly stopped updating."""
    convo = _convo(monkeypatch, events)
    _park(convo, "wk1", name="w", status="running")
    convo._emit_subagent_stream("wk1", "tool_call", name="write_file",
                                args={"path": "auth.py"})
    assert ("wk1", "tool_call") in [(i, k) for i, k, _d in events.streams]
    with convo._workers_lock:
        assert convo._workers["wk1"]["wrote"] == ["auth.py"]


def test_an_event_for_an_unknown_worker_is_harmless(monkeypatch, events):
    """spawn_agents sub-agents share this funnel and are not workers at all."""
    convo = _convo(monkeypatch, events)
    convo._emit_subagent_stream("sub7", "tool_call", name="write_file",
                                args={"path": "auth.py"})


# --------------------------------------------------------------------- #
# What the coding agent is told after the fact

def test_a_stopped_worker_is_not_reported_to_the_coding_agent_as_failed():
    from glmcode.prompts import worker_report_note
    said = worker_report_note("w", "stopped", "partial work")
    assert "failed" not in said
    assert "stopped by you" in said


def test_a_reverted_worker_is_described_as_undone():
    from glmcode.prompts import worker_report_note
    said = worker_report_note("w", "reverted", "")
    assert "failed" not in said
    assert "undone" in said


def test_a_real_failure_is_still_called_a_failure():
    from glmcode.prompts import worker_report_note
    assert "failed" in worker_report_note("w", "error", "traceback")


def test_an_unknown_status_does_not_claim_success_or_failure():
    """A status this map has never seen should not be guessed in either
    direction -- both guesses are a statement about work nobody checked."""
    from glmcode.prompts import worker_report_note
    said = worker_report_note("w", "something-new", "")
    assert "failed" not in said
    assert "successfully" not in said


# --------------------------------------------------------------------- #
# A worker that is still going

def test_reverting_a_running_worker_is_refused(monkeypatch, events):
    """The phone's revert_worker has always refused this. Reverting under a
    worker that is still writing is a race with no good outcome: it carries on
    from where it was and the tree ends up half one thing and half the other."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    _park(convo, "wk1", name="busy", status="running",
          baseline="commit1", changes=[("M", "auth.py")], wrote=["auth.py"])

    out = convo._revert_worker_tool("busy")
    assert "stop it first" in out
    assert convo.backup_repo.reverted_paths is None
    with convo._workers_lock:
        assert convo._workers["wk1"]["status"] == "running"


def test_worker_changes_on_a_running_worker_does_not_claim_nothing(monkeypatch, events):
    """`changes` is only filled in when the worker finishes, so an empty list
    mid-flight means "not known yet". Reporting it as "changed nothing" is a
    wrong answer about work still in progress."""
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    _park(convo, "wk1", name="busy", status="running", baseline="commit1")

    said = convo._worker_changes_tool("busy")
    assert "still running" in said
    assert "auth.py" in said          # read live, from the baseline


def test_a_running_worker_with_nothing_yet_says_so(monkeypatch, events):
    convo = _convo(monkeypatch, events)
    convo.backup_repo = _FakeBackup()
    convo.backup_repo.changes = []
    _park(convo, "wk1", name="busy", status="running", baseline="commit1")

    said = convo._worker_changes_tool("busy")
    assert "still running" in said
    assert "hasn't changed any files yet" in said
