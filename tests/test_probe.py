"""Watching the program think: trace_run.

Driven end to end against real subprocesses, because the whole mechanism is
what CPython does with `sitecustomize` on `PYTHONPATH` at interpreter startup.
A mock of the tracer would only ever agree with whatever I believed that did --
the same reason the SSE-encoding tests drive real `requests` objects.
"""

import sys

import pytest

from glmcode import probe, tools
from glmcode.permissions import PermissionEngine


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    monkeypatch.setattr(probe, "get_workdir", lambda: tmp_path)
    return tmp_path


def write(project, name, text):
    p = project / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


PY = sys.executable


# --------------------------------------------------------------------- #
# The thing it exists for

def test_it_shows_the_value_that_caused_the_failure(project):
    """The whole point: a traceback says which line, and this says WHY —
    replacing the add-a-print-and-run-again loop with one call."""
    write(project, "app.py", (
        "def parse(raw):\n"
        "    return raw.split(',')[1] if ',' in raw else None\n"
        "def total(raw):\n"
        "    n = parse(raw)\n"
        "    return int(n) * 2\n"
        "total('oops')\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "parse(raw='oops')" in out
    assert "parse returned None" in out
    assert "n = None" in out, "the value at the failing frame is the finding"
    assert "TypeError" in out


def test_arguments_and_return_values_are_both_recorded(project):
    write(project, "app.py", (
        "def double(x):\n"
        "    return x * 2\n"
        "double(21)\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "double(x=21)" in out
    assert "double returned 42" in out


def test_the_exception_message_is_kept_not_just_its_class(project):
    """"TypeError" is what the traceback already said. The message is the
    finding."""
    write(project, "app.py", "int(None)\n")
    out = probe.trace_run(f"{PY} app.py")
    assert "TypeError" in out
    assert "NoneType" in out
    assert "TypeError: TypeError(" not in out, "the class must not be printed twice"


# --------------------------------------------------------------------- #
# Scope: somebody else's code is not what anybody asked for

def test_the_standard_library_is_not_traced(project):
    write(project, "app.py", (
        "import json\n"
        "def mine(d):\n"
        "    return json.dumps(d)\n"
        "mine({'a': 1})\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "mine(" in out
    assert "json/encoder" not in out and "iterencode" not in out


def test_focus_narrows_to_one_function(project):
    write(project, "app.py", (
        "def wanted(x):\n"
        "    return x\n"
        "def noise(x):\n"
        "    return x\n"
        "wanted(1)\n"
        "noise(2)\n"))
    out = probe.trace_run(f"{PY} app.py", focus="wanted")
    assert "wanted(" in out
    assert "noise(x=2)" not in out


# --------------------------------------------------------------------- #
# Bounds

def test_a_runaway_trace_is_capped_and_says_so(project, monkeypatch):
    monkeypatch.setattr(probe, "MAX_EVENTS", 50)
    write(project, "app.py", (
        "def tick(i):\n"
        "    return i\n"
        "for i in range(5000):\n"
        "    tick(i)\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "trace stopped after" in out
    assert "focus" in out, "a cap must name the way out of it"


def test_the_whole_result_stays_inside_the_tool_output_cap(project, monkeypatch):
    """An uncapped tool result once ballooned a chat to ~1.5M tokens here.
    A tracer is the easiest way to do it again."""
    monkeypatch.setattr(probe, "MAX_EVENTS", 4000)
    write(project, "app.py", (
        "def tick(i):\n"
        "    return 'x' * 200\n"
        "for i in range(4000):\n"
        "    tick(i)\n"
        "print('y' * 100000)\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert len(out) <= tools.MAX_TOOL_OUTPUT + 500   # _truncate overshoots slightly


def test_a_huge_value_is_summarised_rather_than_printed(project):
    write(project, "app.py", (
        "def big(x):\n"
        "    return x\n"
        "big(list(range(100000)))\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "len=100000" in out
    assert "99999" not in out


# --------------------------------------------------------------------- #
# It must not change the program it is watching

def test_an_object_whose_repr_raises_does_not_break_the_run(project):
    write(project, "app.py", (
        "class Hostile:\n"
        "    def __repr__(self):\n"
        "        raise RuntimeError('no repr for you')\n"
        "def take(x):\n"
        "    return 'survived'\n"
        "print(take(Hostile()))\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "exit code: 0" in out
    assert "survived" in out


def test_a_projects_own_sitecustomize_is_not_replaced(project, monkeypatch):
    """PYTHONPATH is PREPENDED to. Silently replacing a project's own
    sitecustomize would change how the program under test starts up, which is
    the one thing a tracer must not do."""
    other = project / "theirs"
    other.mkdir()
    (other / "sitecustomize.py").write_text(
        "import os; os.environ['THEIRS_RAN'] = '1'\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(other))
    write(project, "app.py", (
        "import os\n"
        "def check():\n"
        "    return os.environ.get('THEIRS_RAN')\n"
        "print('theirs:', check())\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "theirs: 1" in out, "the project's own sitecustomize must still run"
    assert "check(" in out, "and ours must still trace"


# --------------------------------------------------------------------- #
# Honesty

def test_a_non_python_command_says_so_rather_than_looking_like_it_worked(project):
    out = probe.trace_run("echo hello")
    assert "hello" in out
    assert "no trace collected" in out
    assert "only sees Python" in out


def test_a_command_that_hangs_is_given_up_on_with_a_reason(project):
    write(project, "app.py", "import time\ntime.sleep(30)\n")
    out = probe.trace_run(f"{PY} app.py", timeout_seconds=2)
    assert "did not finish" in out
    assert "focus" in out or "timeout_seconds" in out


def test_a_command_that_cannot_start_is_reported(project):
    out = probe.trace_run("")
    assert "needs a command" in out


def test_the_exit_code_is_always_reported(project):
    """That a code is reported, not which one.

    The command goes through the same shell run_command uses, and PowerShell
    does not propagate a child's exit code -- SystemExit(3) arrives as 1 on
    Windows. Asserting the exact value pinned the shell's behaviour rather
    than this tool's, and failed on the one platform CI actually runs it on.
    Probe is deliberately no different from run_command here.
    """
    write(project, "app.py", "raise SystemExit(3)\n")
    out = probe.trace_run(f"{PY} app.py")
    assert "exit code: " in out
    assert "exit code: 0" not in out, "a failure must not read as success"


# --------------------------------------------------------------------- #
# Secrets must not ride out in a trace

def test_a_secret_in_a_traced_value_is_redacted(project):
    write(project, "app.py", (
        "def authenticate(token):\n"
        "    return bool(token)\n"
        "authenticate('ghp_" + "a" * 40 + "')\n"))
    out = probe.trace_run(f"{PY} app.py")
    assert "ghp_" + "a" * 40 not in out
    assert "redacted" in out


def test_redaction_reuses_the_projects_own_definition():
    """Two ideas of what a secret looks like is one of them being wrong."""
    from glmcode.tools import _SECRET_PATTERNS
    text = "key = 'AKIA" + "A" * 16 + "'"
    assert "AKIA" not in probe._redact(text)
    assert any(label == "AWS access key id" for label, _ in _SECRET_PATTERNS)


# --------------------------------------------------------------------- #
# It runs a shell command, so it is gated like one

def test_it_is_gated_exactly_like_run_command():
    """Its absence from SHELL_TOOL_NAMES would be a permission BYPASS, not a
    missing prompt: everything that gates a shell command is keyed on that
    set, so a shell tool outside it runs anything with no prompt at all."""
    assert "trace_run" in tools.SHELL_TOOL_NAMES
    assert "trace_run" not in tools.READONLY_TOOLS

    asked = []

    def ask(title, preview, always_label=None):
        asked.append(title)
        return "no"

    decision = PermissionEngine(mode="ask").check(
        "trace_run", {"command": "rm -rf /"}, ask)
    assert decision.allowed is False
    assert asked, "it must prompt"
