"""Did CI pass? Asked from the machine that pushed, not from a browser.

The desktop has a shell, so it can run the tests -- but not the ones that run
on a RUNNER, and those are the ones that gate a merge. Asked "did CI pass" the
agent could only say to go and look, which is the same answer it gave before it
could push at all.

The phone has the same tool for the same reason. These are two implementations
of one shape, and the rules below -- what counts as a pass, what "nothing
reported" means, never promising a log -- are the part that must not drift,
because a model that learns them on one device carries them to the other.
"""

import subprocess
import sys
import types

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

import pytest

from glmcode import githubsync, tools


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git checkout with a GitHub-looking origin and one commit."""
    p = tmp_path / "proj"
    p.mkdir()
    _git(["init", "-q", "-b", "work"], p)
    _git(["config", "user.email", "t@example.test"], p)
    _git(["config", "user.name", "T"], p)
    (p / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(["add", "-A"], p)
    _git(["commit", "-qm", "first"], p)
    _git(["remote", "add", "origin", "https://github.com/you/app.git"], p)
    monkeypatch.setattr(tools, "_resolve", lambda path: p)
    monkeypatch.setattr(githubsync, "load_token", lambda host="github.com": "tok")
    return p


def _runs(monkeypatch, rows, capture=None):
    def fake(token, owner, repo, ref):
        if capture is not None:
            capture.append((token, owner, repo, ref))
        return rows
    monkeypatch.setattr(githubsync, "check_runs", fake)


def _run(name, status="completed", conclusion="success", **kw):
    r = {"name": name, "status": status, "conclusion": conclusion,
         "url": "https://github.com/you/app/runs/1", "title": "", "summary": ""}
    r.update(kw)
    return r


# --------------------------------------------------------------------- #

def test_all_green_says_so(repo, monkeypatch):
    _runs(monkeypatch, [_run("tests (ubuntu)"), _run("tests (windows)")])
    out = tools.check_ci()
    assert "2 passed, 0 failed, 0 still running" in out
    assert "Everything green" in out


def test_a_failure_is_named_with_its_summary_and_a_link(repo, monkeypatch):
    _runs(monkeypatch, [
        _run("tests (windows)", conclusion="failure",
             title="1 failed", summary="test_grep_stops_once_it_has_enough"),
        _run("tests (ubuntu)"),
    ])
    out = tools.check_ci()
    assert "FAILED  tests (windows)" in out
    assert "1 failed" in out
    assert "test_grep_stops_once_it_has_enough" in out
    assert "https://github.com/you/app/runs/1" in out


def test_a_run_still_going_is_not_reported_as_a_pass(repo, monkeypatch):
    _runs(monkeypatch, [_run("browser-ui", status="in_progress", conclusion=""),
                        _run("tests (ubuntu)")])
    out = tools.check_ci()
    assert "1 passed, 0 failed, 1 still running" in out
    assert "Everything green" not in out
    assert "ask again once the rest finish" in out


def test_skipped_and_neutral_are_not_failures(repo, monkeypatch):
    """A conditional job that did not need to run is not a broken build, and
    calling it one makes this cry wolf on every repo that has one."""
    _runs(monkeypatch, [_run("a", conclusion="skipped"), _run("b", conclusion="neutral")])
    assert "2 passed, 0 failed" in tools.check_ci()


def test_a_timed_out_run_is_a_failure_and_says_which(repo, monkeypatch):
    _runs(monkeypatch, [_run("slow", conclusion="timed_out")])
    assert "FAILED  slow (timed_out)" in tools.check_ci()


def test_nothing_reported_covers_both_reasons(repo, monkeypatch):
    """"Not started yet" and "this repo has no CI" look identical from here,
    and saying only the first sends someone off to wait for a run that is never
    coming."""
    _runs(monkeypatch, [])
    out = tools.check_ci()
    assert "No checks have reported" in out
    assert "may not have started" in out
    assert "none configured" in out


def test_it_asks_about_the_current_branch(repo, monkeypatch):
    seen = []
    _runs(monkeypatch, [_run("a")], capture=seen)
    tools.check_ci()
    assert seen == [("tok", "you", "app", "work")]


def test_another_ref_can_be_named(repo, monkeypatch):
    seen = []
    _runs(monkeypatch, [_run("a")], capture=seen)
    tools.check_ci(ref="main")
    assert seen[0][3] == "main"


def test_unpushed_commits_are_called_out(repo, monkeypatch):
    """The one thing the desktop knows that the phone does not: whether what is
    on the runner is what is on this disk. A green answer about a commit two
    pushes ago is the most confidently wrong output this tool could produce."""
    up = repo.parent / "up.git"
    _git(["init", "-q", "--bare", str(up)], repo)
    _git(["remote", "set-url", "origin", str(up)], repo)
    _git(["push", "-q", "-u", "origin", "work"], repo)
    _git(["remote", "set-url", "origin", "https://github.com/you/app.git"], repo)
    (repo / "b.txt").write_text("more\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "second"], repo)

    _runs(monkeypatch, [_run("tests")])
    out = tools.check_ci()
    assert "1 commit here has not been pushed" in out
    assert "older version of work" in out
    assert "Everything green" in out          # still reports what it found


def test_a_named_ref_gets_no_unpushed_warning(repo, monkeypatch):
    """Asking about another branch says nothing about this one's state."""
    _runs(monkeypatch, [_run("tests")])
    assert "not been pushed" not in tools.check_ci(ref="main")


def test_a_summary_is_capped(repo, monkeypatch):
    """A check's summary is written for a web page and can run to thousands of
    lines; unbounded it is a whole turn's context spent on one failure."""
    _runs(monkeypatch, [_run("big", conclusion="failure",
                             summary="\n".join(f"line {i}" for i in range(4000)))])
    out = tools.check_ci()
    assert "truncated" in out
    assert len(out) < 4000, len(out)


def test_it_never_offers_a_log(repo, monkeypatch):
    """A job log is served as a redirect to blob storage and can be tens of
    megabytes. Promising one would be a tool that fails on the case it exists
    for, so the description must not imply it either."""
    said = [s["function"]["description"] for s in tools.TOOL_SCHEMAS
            if s["function"]["name"] == "check_ci"][0]
    assert "log" not in said.lower()


# ---- the ways it cannot answer, each naming the actual state ---------------

def test_not_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_resolve", lambda path: tmp_path)
    assert "not a git checkout" in tools.check_ci()


def test_no_github_remote(tmp_path, monkeypatch):
    p = tmp_path / "plain"
    p.mkdir()
    _git(["init", "-q"], p)
    monkeypatch.setattr(tools, "_resolve", lambda path: p)
    assert "no GitHub remote" in tools.check_ci()


def test_no_token_points_at_where_to_add_one(repo, monkeypatch):
    """Without this the tool looks broken, and the fix is one field in a
    settings panel the model can point at."""
    monkeypatch.setattr(githubsync, "load_token", lambda host="github.com": None)
    out = tools.check_ci()
    assert "No GitHub token" in out
    assert "Settings" in out


def test_a_detached_head_says_so(repo, monkeypatch):
    _git(["checkout", "-q", "--detach"], repo)
    _runs(monkeypatch, [_run("a")])
    assert "detached HEAD" in tools.check_ci()


def test_a_refused_request_is_reported_not_raised(repo, monkeypatch):
    def boom(*a, **k):
        raise githubsync.GitHubError("GitHub rejected the token")
    monkeypatch.setattr(githubsync, "check_runs", boom)
    out = tools.check_ci()
    assert "Couldn't read the checks" in out
    assert "rejected the token" in out


# ---- and the two devices agree about what an answer looks like ------------

def test_the_phone_and_the_desktop_use_the_same_passing_set():
    """A model that learns "skipped is fine" on one device carries it to the
    other. The two are separate implementations; this is the rule they share."""
    core = (tools.Path(__file__).resolve().parent.parent
            / "mobile" / "agent-core.js").read_text(encoding="utf-8")
    for word in tools._CI_PASSING:
        assert f'"{word}"' in core, word


def test_check_ci_is_read_only():
    assert "check_ci" in tools.READONLY_TOOLS
