"""The shell tool runs a real command, on whatever platform this is.

Every other test of the command tools substitutes a fake Popen -- necessarily,
because they are testing the stop path and the timeout path, and neither wants
a real process. The cost of that was a blind spot exactly the size of the bug:
run_powershell was hard-coded to exec `powershell`, so on macOS and Linux it
raised "Failed to start PowerShell" for every command it was ever given, and
the suite was green over it on ubuntu-latest -- the only platform CI actually
runs.

So these tests mock nothing. They run `echo` through the real tool, in the
real shell, and read the real output. That is the assertion that was missing,
and it is worth exactly one second of suite time.

The other half is the system prompt. Naming the wrong shell makes the agent
write commands that cannot run here, so what the prompt claims and what the
tool execs are pinned to each other rather than maintained in parallel.
"""

import os
import subprocess
import sys
import time

import pytest

from glmcode import tools
from glmcode.prompts import build_system_prompt

pytestmark = pytest.mark.usefixtures("_workdir")


@pytest.fixture
def _workdir(tmp_path):
    # set_workdir pins a thread-local that pytest's single thread would carry
    # into every test after this file, so it is put back rather than left set.
    previous = tools.get_workdir()
    tools.set_workdir(tmp_path)
    yield tmp_path
    tools.set_workdir(previous)


# ------------------------------------------------------- it actually runs --

def test_a_real_command_runs_and_returns_its_output():
    """The bug, stated as a test: this raised ToolError on two of the three
    platforms because the tool could not start its interpreter."""
    out = tools.run_command("echo hello")
    assert "hello" in out
    assert "[exit code: 0]" in out


def test_a_failing_command_reports_its_exit_code():
    out = tools.run_command("exit 3")
    assert "[exit code: 3]" in out


def test_the_command_runs_in_the_project_working_directory(_workdir):
    (_workdir / "marker.txt").write_text("found me", encoding="utf-8")
    listing = tools.run_command("dir marker.txt" if os.name == "nt" else "ls marker.txt")
    assert "marker.txt" in listing


def test_stderr_comes_back_labelled():
    out = tools.run_command(
        "Write-Error 'boom'" if os.name == "nt" else "echo boom 1>&2")
    assert "boom" in out


def test_the_check_command_runner_agrees_with_the_tool():
    """run_check_command is the deterministic path the green loop uses. It had
    the platform branch all along, which is how the two came to disagree."""
    code, text = tools.run_check_command("echo hello")
    assert code == 0 and "hello" in text


# -------------------------------------------------- it names itself right --

def test_the_shell_name_matches_the_shell_that_gets_exec_ed():
    argv = tools._shell_argv("echo hi")
    if os.name == "nt":
        assert tools.shell_name() == "Windows PowerShell"
        assert argv[0] == "powershell"
    else:
        # Down to bash-vs-sh: a model told "bash" writes [[ ]] and arrays, and
        # on a Debian /bin/sh is dash, which rejects them.
        assert argv[0].endswith("bash") == (tools.shell_name() == "bash")
        assert argv[1] == "-c"


def test_the_system_prompt_names_the_shell_the_tool_will_use():
    lines = [l for l in build_system_prompt().splitlines() if l.startswith("Shell:")]
    assert lines == [f"Shell: {tools.shell_name()}"]


@pytest.mark.skipif(os.name == "nt", reason="the reverse claim is the one that bit")
def test_nothing_tells_a_posix_machine_it_has_powershell():
    """The system prompt said "a POSIX shell (bash/zsh)" while the tool tried
    to exec powershell. Whichever way round that mismatch sits, it produces
    commands that cannot run."""
    prompt = build_system_prompt()
    assert "Windows PowerShell" not in prompt
    schema = next(s for s in tools.TOOL_SCHEMAS
                  if s["function"]["name"] == "run_command")
    assert "PowerShell" not in schema["function"]["description"]


# ------------------------------------------------------------ the rename --

def test_the_model_is_offered_the_new_name_only():
    names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    assert "run_command" in names
    assert "run_powershell" not in names


def test_the_old_name_still_resolves_for_saved_sessions():
    """A chat saved before the rename replays tool calls by name. Dropping the
    old key would turn every one of them into "unknown tool"."""
    assert tools.TOOL_FUNCTIONS["run_powershell"] is tools.TOOL_FUNCTIONS["run_command"]
    assert "hello" in tools.execute_tool("run_powershell", {"command": "echo hello"})


def test_the_permission_engine_recognises_both_names():
    """It gates on the tool name, so the retired one must still be understood
    as a shell command -- otherwise an old session's `rm -rf` falls through to
    the generic branch instead of the command prompt."""
    from glmcode.permissions import PermissionEngine

    for name in ("run_command", "run_powershell"):
        eng = PermissionEngine(mode="autoedit")
        asked = []
        eng.check(name, {"command": "rm -rf build"},
                  lambda *a, **k: (asked.append(a), "n")[1])
        assert asked, f"{name} should have prompted"


# ------------------------------------------------- long-lived commands ----

def _script(directory, body: str) -> str:
    """Write a throwaway python script and return a command that runs it.

    Deliberately a file rather than `python -c "..."`: the command is one
    argument to Popen, and getting a multi-line, nested-quote string through
    both Windows' list2cmdline AND PowerShell's own parser intact is a coin
    toss. Nothing here is testing quoting, so it shouldn't be exposed to it.
    """
    path = directory / f"job_{abs(hash(body)) % 10_000}.py"
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


_SPINS = "import time\nprint('up', flush=True)\nwhile True: time.sleep(0.1)\n"


def test_a_background_process_starts_reports_and_stops(_workdir):
    started = tools.run_background(_script(_workdir, _SPINS))
    assert "status: running" in started
    bg_id = started.split("'")[1]
    try:
        assert "up" in started or "up" in tools.read_output(bg_id)
        assert "stopped" in tools.stop_process(bg_id)
        deadline = time.time() + 5
        while time.time() < deadline:
            if tools._bg_processes[bg_id].status() != "running":
                break
            time.sleep(0.1)
        assert tools._bg_processes[bg_id].status() != "running"
    finally:
        record = tools._bg_processes.get(bg_id)
        if record and record.status() == "running":
            tools._terminate_process_tree(record.proc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_process_we_did_not_start_is_never_signalled_as_a_group():
    """Guarding this is not defensive tidiness -- the unguarded version took
    down the test runner.

    Every other test of these tools substitutes a fake process object with an
    invented pid. Killing "the process group of pid 4321" reaches whatever
    really holds 4321, which during a test run is something in pytest's own
    group: the suite SIGTERM'd itself at 28%. The same hazard exists in
    production the moment a real pid is reaped and recycled.
    """
    killed = []

    class FakeProc:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            killed.append("terminate")

    monkeyed = FakeProc()
    assert tools._own_process_group(monkeyed) is None, \
        "a process this module never started must not be group-signalled"
    tools._terminate_process_tree(monkeyed)
    assert killed == ["terminate"], "it should fall through to terminate()"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_an_exited_process_is_not_signalled_by_group(_workdir):
    """A pid is only unique while its process lives. Signalling the group of
    one that already exited can reach whatever inherited the number."""
    started = tools.run_background("echo done")
    bg_id = started.split("'")[1]
    proc = tools._bg_processes[bg_id].proc
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "the command should have finished"
    assert tools._own_process_group(proc) is None


@pytest.mark.skipif(os.name == "nt", reason="taskkill /T covers this on Windows")
def test_stopping_a_background_process_kills_what_it_spawned(_workdir):
    """The reason the shell is started in its own process group. proc.terminate()
    signals only the shell, so a stopped `npm run dev` would leave the actual
    server holding the port -- invisibly, since the app believes it stopped it."""
    started = tools.run_background(_script(_workdir, _SPINS) + " & echo spawned; wait")
    bg_id = started.split("'")[1]
    proc = tools._bg_processes[bg_id].proc
    pgid = os.getpgid(proc.pid)

    def alive() -> set:
        # Zombies are dead and merely unreaped, so they must not count as
        # survivors -- reading them as running is what makes this test lie.
        out = subprocess.run(["ps", "-o", "pid=,stat=", "-g", str(pgid)],
                             capture_output=True, text=True).stdout
        return {line.split()[0] for line in out.splitlines()
                if line.split() and not line.split()[1].startswith("Z")}

    deadline = time.time() + 5
    while time.time() < deadline and len(alive()) < 2:
        time.sleep(0.1)
    assert len(alive()) >= 2, "the shell should have spawned a child to kill"

    tools.stop_process(bg_id)
    deadline = time.time() + 5
    while time.time() < deadline and alive():
        time.sleep(0.1)
    assert not alive(), "the spawned child outlived the stop"
