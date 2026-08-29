"""The classifier that decides what runs WITHOUT asking, and what plan mode is.

`is_readonly_command` is not only a convenience. It is what plan mode enforces
itself with -- documented as "a hard deny with corrective feedback, regardless
of ask/autoedit/yolo mode or session allowlists" -- so a command it waves
through writes files during a turn that promised only to explore. It is also
what lets a shell command past the per-path rules that protect `.env` and
migrations, since those apply to file-WRITE tools and a shell command is not
one.

The module's own bar is "provably read-only" and "when in doubt, we ask".
Three kinds of thing were getting through.
"""

import pytest

from glmcode.permissions import PermissionEngine, is_readonly_command


def _never(*a, **k):
    raise AssertionError("this should have asked, and did not")


# ---- a prefix that starts another command -------------------------------

RUNS_SOMETHING_ELSE = [
    "env rm -rf build",
    "env FOO=1 rm x",
    "command rm important.txt",
    "command -p rm x",
    "nice -n 10 rm x",
    "nohup rm x",
    "stdbuf -o0 rm x",
    "env env env rm x",          # bounded recursion, not a way through
    "env -S 'rm x'",             # -S takes a command as a STRING
]


@pytest.mark.parametrize("cmd", RUNS_SOMETHING_ELSE)
def test_a_prefix_is_judged_by_what_it_runs(cmd):
    """`env` and `command` were in the safe list, so `env rm -rf build` was
    classified as reading. They are not commands; they are ways of starting
    one."""
    assert is_readonly_command(cmd) is False, cmd


STILL_READS = [
    "env NODE_ENV=production npm ls",   # the ordinary reason to write `env`
    "env",                              # prints the environment
    "env -u FOO git status",
    "env -i printenv",
    "nice -n 10 git status",
    "command -v python",                # a lookup: -v stops it running anything
    "command -V git",
    "env -- cat file.txt",
]


@pytest.mark.parametrize("cmd", STILL_READS)
def test_refusing_outright_would_have_been_wrong_too(cmd):
    """`env NODE_ENV=x npm ls` reads exactly as much as `npm ls` does. The
    prefix is stripped and what is left is judged on its own merits."""
    assert is_readonly_command(cmd) is True, cmd


# ---- a safe command with an output operand ------------------------------

WRITES_A_FILE = [
    "sort -o wiped.txt input.txt",
    "sort --output=wiped.txt input.txt",
    "sort -owiped.txt input.txt",       # joined short form
    "uniq input.txt overwritten.txt",   # second operand is the OUTPUT
    "xxd -r dump.hex restored.bin",
]


@pytest.mark.parametrize("cmd", WRITES_A_FILE)
def test_a_reader_that_was_handed_somewhere_to_write(cmd):
    """Redirection was already refused; these need no redirection. They take
    the output file as an argument."""
    assert is_readonly_command(cmd) is False, cmd


READS_ONLY = [
    "sort input.txt",
    "sort -k2 -t: input.txt",           # value flags are not operands
    "sort -r",
    "uniq -c input.txt",
    "uniq -f 2 input.txt",              # -f takes a number, not the output
    "xxd input.bin",
    "xxd -l 64 input.bin",
]


@pytest.mark.parametrize("cmd", READS_ONLY)
def test_the_same_commands_still_read(cmd):
    assert is_readonly_command(cmd) is True, cmd


# ---- what it actually cost ----------------------------------------------

def test_plan_mode_could_be_made_to_write():
    """Plan mode's guarantee is that a turn only explores. It is enforced by
    this classifier, so a hole in it is a hole in the guarantee."""
    eng = PermissionEngine(mode="ask", plan_only=True)
    d = eng.check("run_command", {"command": "env rm -rf build"}, lambda *a, **k: "n")
    assert not d.allowed
    assert "plan mode" in d.feedback.lower()


def test_autoedit_asks_before_running_one():
    asked = []
    eng = PermissionEngine(mode="autoedit")
    eng.check("run_command", {"command": "sort -o .env from.txt"},
              lambda *a, **k: (asked.append(1), "n")[1])
    assert asked, "it ran a write with no prompt"


def test_a_genuine_read_still_never_asks():
    eng = PermissionEngine(mode="autoedit")
    assert eng.check("run_command", {"command": "env NODE_ENV=x npm ls"}, _never).allowed
