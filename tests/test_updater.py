"""The Update button: `git pull` with the sharp edges off.

The app is installed by cloning it, so an update is a pull and a restart. Both
halves fail in ways that are invisible from a button, and a button that
half-works is worse than none: it leaves someone's app directory in a state
they did not ask for and cannot see.

These build real git repositories. A faked `git` would only prove the parsing.
"""

import subprocess
import sys
import types

import pytest

from glmcode import updater


def git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "PATH": __import__("os").environ.get("PATH", "")}
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.fixture
def clone(tmp_path, monkeypatch):
    """A clone with an upstream that is one commit ahead."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "app.py").write_text("v1\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "first")

    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    monkeypatch.setattr(updater, "APP_ROOT", work)
    return types.SimpleNamespace(origin=origin, work=work)


def advance(origin, message="a new thing"):
    (origin / "app.py").write_text("v2\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", message)


# --------------------------------------------------------------------- #
# Looking, without touching

def test_up_to_date_says_so(clone):
    st = updater.check()
    assert st["ok"] and st["behind"] == 0
    assert "Already up to date" in st["reason"]


def test_it_finds_an_update_and_says_what_is_in_it(clone):
    advance(clone.origin, "fix the thing that was broken")
    st = updater.check()
    assert st["ok"] and st["behind"] == 1
    assert any("fix the thing" in c for c in st["changes"])


def test_checking_changes_nothing(clone):
    """It is called every time the panel opens. It must be safe to call when
    the user only wanted to look."""
    advance(clone.origin)
    before = (clone.work / "app.py").read_text()
    updater.check()
    assert (clone.work / "app.py").read_text() == before


def test_local_changes_stop_it_rather_than_being_overwritten(clone):
    """Someone editing their own copy is the person most likely to press this,
    and a pull that clobbered their work would be unrecoverable from a button."""
    advance(clone.origin)
    (clone.work / "app.py").write_text("my own edit\n", encoding="utf-8")
    st = updater.check()
    assert st["ok"] is False and st["dirty"] == 1
    assert "uncommitted" in st["reason"]
    assert updater.pull()["ok"] is False
    assert (clone.work / "app.py").read_text() == "my own edit\n"


def test_a_folder_that_is_not_a_checkout_says_that(tmp_path, monkeypatch):
    """A copied folder or an unzipped release has nothing to pull from, and
    'update failed' would leave someone with no idea why."""
    monkeypatch.setattr(updater, "APP_ROOT", tmp_path)
    st = updater.check()
    assert st["ok"] is False and "git checkout" in st["reason"]


def test_a_detached_head_is_refused(clone):
    sha = git(clone.work, "rev-parse", "HEAD").strip()
    git(clone.work, "checkout", "-q", sha)
    st = updater.check()
    assert st["ok"] is False and "detached" in st["reason"].lower()


def test_a_branch_with_no_upstream_is_refused(clone):
    git(clone.work, "checkout", "-q", "-b", "mine")
    st = updater.check()
    assert st["ok"] is False and "tracking" in st["reason"]


# --------------------------------------------------------------------- #
# Taking it

def test_pull_brings_the_new_code_in(clone):
    advance(clone.origin)
    res = updater.pull()
    assert res["ok"] and res["updated"] and res["count"] == 1
    assert (clone.work / "app.py").read_text() == "v2\n"


def test_pull_with_nothing_to_do_is_not_an_error(clone):
    res = updater.pull()
    assert res["ok"] and res["updated"] is False


def test_a_diverged_branch_refuses_instead_of_merging(clone):
    """--ff-only on purpose. A merge commit created by a button in someone's
    app directory is not something they asked for, and a CONFLICTED merge
    leaves the app in a state it cannot run from. Refusing is recoverable."""
    advance(clone.origin, "upstream work")
    (clone.work / "app.py").write_text("local work\n", encoding="utf-8")
    git(clone.work, "add", "-A")
    git(clone.work, "commit", "-q", "-m", "local work")
    res = updater.pull()
    assert res["ok"] is False
    assert "wouldn't apply cleanly" in res["reason"]
    assert (clone.work / "app.py").read_text() == "local work\n"


# --------------------------------------------------------------------- #
# Restarting

def test_the_restart_uses_this_interpreter():
    """Not a hard-coded 'python': on Windows that may be a different install,
    or absent from PATH entirely."""
    assert updater.restart_command()[0] == sys.executable
    assert updater.restart_command()[1:] == ["-m", "glmcode.gui"]


def test_the_new_process_is_detached(monkeypatch):
    """It has to outlive this one -- the caller closes the window right after.
    A child tied to a dying parent means the update just quits the app."""
    seen = {}
    monkeypatch.setattr(updater.subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv, kw=kw))
    assert updater.spawn_restart() is True
    if sys.platform == "win32":
        assert seen["kw"]["creationflags"] & 0x00000008     # DETACHED_PROCESS
    else:
        assert seen["kw"]["start_new_session"] is True


def test_a_restart_that_cannot_start_is_reported_not_swallowed(monkeypatch):
    def boom(argv, **kw):
        raise OSError("nope")
    monkeypatch.setattr(updater.subprocess, "Popen", boom)
    assert updater.spawn_restart() is False


# --------------------------------------------------------------------- #
# The button

def _api(monkeypatch, chats):
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app
    api = gui_app.Api.__new__(gui_app.Api)
    api._chats = chats
    return api


def test_it_refuses_while_a_turn_is_running(monkeypatch):
    """Restarting mid-turn loses whatever the agent was part-way through, and
    an update is never so urgent that it cannot wait for a reply to finish."""
    import threading
    lock = threading.Lock()
    lock.acquire()
    chats = {"s1": types.SimpleNamespace(title="Fixing login", sid="s1",
                                         turn_lock=lock)}
    api = _api(monkeypatch, chats)
    res = api.update_apply()
    assert res["ok"] is False
    assert "Fixing login" in res["reason"] and "still working" in res["reason"]
