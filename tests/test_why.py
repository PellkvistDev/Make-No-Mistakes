"""why(): the reasons, not just the code.

Source code is the one artefact that cannot tell you what was already tried and
reverted -- a line that came back looks identical to a line never touched. This
project writes the reason down every time, in the comment above the code and in
the commit body (which for a squash-merged PR is the whole PR description), and
until now the agent could reach none of it.

These tests build real git repositories, because the whole tool is a reading of
what git actually stores; a fake `git log -L` would only prove the formatter
works.
"""

import subprocess

import pytest

from glmcode import tools
from glmcode.errors import ToolError


def git(repo, *args, **env):
    e = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T",
         "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null",
         "GIT_CONFIG_SYSTEM": "/dev/null", "PATH": __import__("os").environ.get("PATH", "")}
    e.update(env)
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A repo whose history contains a thing that was tried and reverted --
    which is the case the tool exists for."""
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")

    f = tmp_path / "kb.js"
    f.write_text("const bar = 0;\nfunction lift() {\n  return 1;\n}\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "First cut of the keyboard lift",
        "-m", "Measures the hidden viewport and adds room for the accessory bar.")

    f.write_text("const bar = 0;\nfunction lift() {\n  return predict();\n}\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "Predict the lift at focusin (#64)",
        "-m", "Remember the keyboard height and apply it the moment a field is focused.")

    f.write_text("const bar = 0;\nfunction lift() {\n  return 1;\n}\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "Stop predicting the lift at focusin (#65)",
        "-m", "TRIED and reverted. A remembered height ends up holding a frame of the\n"
              "keyboard sliding AWAY, and an already-open keyboard never fires a resize\n"
              "to correct the guess.\n\nCo-Authored-By: Someone <x@y>")
    return tmp_path


# --------------------------------------------------------------------- #
# The point of the whole thing

def test_it_surfaces_the_approach_that_was_tried_and_reverted(repo):
    """Line 3 reads `return 1;` and looks like it was never anything else.
    Everything about why it is not `predict()` lives in the history."""
    out = tools.why("kb.js", 3)
    assert "Stop predicting the lift at focusin (#65)" in out
    assert "TRIED and reverted" in out
    assert "never fires a resize" in out
    # And the attempt itself, so the agent sees both halves.
    assert "Predict the lift at focusin (#64)" in out


def test_it_reads_the_lines_asked_about_and_not_the_whole_file(repo):
    """Line 1 has its own history and was never part of the lift argument.
    A tool that answered with the file's whole log would bury the answer."""
    out = tools.why("kb.js", 1)
    assert "First cut of the keyboard lift" in out
    assert "#65" not in out


def test_a_range_is_honoured(repo):
    out = tools.why("kb.js", 2, end_line=4)
    assert "#65" in out and "kb.js:2-4" in out


def test_commit_trailers_are_dropped(repo):
    """Co-Authored-By is plumbing. It is not a reason and it costs context."""
    assert "Co-Authored-By" not in tools.why("kb.js", 3)


def test_no_line_gives_the_file_history(repo):
    out = tools.why("kb.js")
    assert "WHAT CHANGED THIS FILE" in out
    assert "#65" in out and "First cut" in out


def test_it_says_a_reverted_commit_is_a_warning(repo):
    """Without this the output reads as a changelog, and a changelog is
    something you skim rather than something you act on."""
    assert "not to do it again" in tools.why("kb.js", 3)


# --------------------------------------------------------------------- #
# What the code says about itself

def test_the_comment_directly_above_the_line(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "a.py").write_text(
        "x = 1\n\n# 344px is what the phone reports, and the accessory bar is\n"
        "# NOT in that number -- it is painted over the web view.\nBAR = 58\n",
        encoding="utf-8")
    out = tools.why("a.py", 5)
    assert "accessory bar" in out and "painted over" in out


def test_a_blank_line_stops_the_comment_run(tmp_path, monkeypatch):
    """A comment separated from the code is usually about something else, and
    attributing it here would read exactly like an answer while being wrong."""
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "a.py").write_text(
        "# about something else entirely\n\nBAR = 58\n", encoding="utf-8")
    out = tools.why("a.py", 3)
    assert "something else entirely" not in out


def test_the_docstring_under_a_multi_line_signature(tmp_path, monkeypatch):
    """Signatures here routinely wrap, so the docstring is not at def+1 -- and
    in this codebase the reason is in the docstring as often as in a comment."""
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "a.py").write_text(
        "def f(a,\n      b,\n      c):\n"
        '    """The lock is TRIED, never taken: appending under a running turn\n'
        '    is how you get a tool_call with no matching reply."""\n'
        "    return a\n",
        encoding="utf-8")
    out = tools.why("a.py", 6)
    assert "TRIED, never taken" in out
    assert "no matching reply" in out


# --------------------------------------------------------------------- #
# Failing usefully

def test_a_file_outside_git_says_so_rather_than_erroring(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "loose.txt").write_text("hi\n", encoding="utf-8")
    out = tools.why("loose.txt")
    assert "Not inside a git repository" in out


def test_a_line_past_the_end_says_how_long_the_file_is(repo):
    out = tools.why("kb.js", 900)
    assert "has 4 lines" in out and "no line 900" in out


def test_a_missing_file_is_an_error(repo):
    with pytest.raises(ToolError):
        tools.why("nope.js", 1)


def test_a_directory_is_an_error(repo):
    with pytest.raises(ToolError):
        tools.why(".", 1)


def test_an_uncommitted_file_says_so(repo):
    (repo / "new.js").write_text("hello\n", encoding="utf-8")
    assert "No commit has touched this yet" in tools.why("new.js", 1)


# --------------------------------------------------------------------- #
# Wiring

def test_it_is_offered_to_the_model_and_costs_no_permission():
    """It only reads git history, so gating it behind a prompt would train the
    user to approve things -- and the whole value is that it gets called
    BEFORE an edit, on a hunch, not after a decision."""
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    assert "why" in names
    assert tools.TOOL_FUNCTIONS["why"] is tools.why
    assert "why" in tools.READONLY_TOOLS


def test_the_prompt_says_when_to_reach_for_it():
    """A tool the model never thinks to call is not a feature. The trigger is
    the feeling that a line looks unnecessary."""
    from glmcode.prompts import SYSTEM_PROMPT as sp
    assert "why(path, line)" in sp
    assert "tried and reverted" in sp


def test_output_is_capped(repo):
    assert len(tools.why("kb.js", 3, max_commits=20)) <= tools.MAX_TOOL_OUTPUT + 200


def test_a_neighbouring_function_is_not_offered_as_the_reason(tmp_path, monkeypatch):
    """The nearest def ABOVE a line is often the one that already ended -- a
    comment between two functions belongs to the one below it. Handing back the
    previous function's docstring reads exactly like an answer, which makes it
    worse than saying nothing."""
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "a.py").write_text(
        "def earlier():\n"
        '    """Nothing whatsoever to do with the thing below."""\n'
        "    return 1\n"
        "\n"
        "\n"
        "# The real reason, and it is about what follows.\n"
        "LIMIT = 344\n",
        encoding="utf-8")
    out = tools.why("a.py", 7)
    assert "The real reason" in out
    assert "Nothing whatsoever" not in out


def test_the_enclosing_function_still_counts_when_you_are_inside_it(tmp_path, monkeypatch):
    """The guard must not throw away the case it exists to serve."""
    monkeypatch.setattr(tools, "get_workdir", lambda: tmp_path)
    (tmp_path / "a.py").write_text(
        "def outer():\n"
        '    """The lock is TRIED, never taken."""\n'
        "    x = 1\n"
        "    return x\n",
        encoding="utf-8")
    assert "TRIED, never taken" in tools.why("a.py", 4)
