"""Per-chat shadow-git backups: snapshot, revert, and never touching the
project's own real .git."""

import shutil

import pytest

import glmcode.backup as backup

pytestmark = pytest.mark.skipif(not backup.available(), reason="git not installed")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "project"
    proj.mkdir()
    # Simulate the user's own real git repo living in the project.
    real_git = proj / ".git"
    (real_git / "objects").mkdir(parents=True)
    (real_git / "config").write_text("THE USER'S REAL GIT CONFIG")
    (real_git / "objects" / "blob").write_text("real object")
    return proj


def test_snapshot_revert_roundtrip(project):
    repo = backup.BackupRepo("sess-1", project)

    h1 = repo.snapshot("first message")
    (project / "a.txt").write_text("version 1")

    h2 = repo.snapshot("second message")
    (project / "a.txt").write_text("version 2")
    (project / "b.txt").write_text("from turn 2")

    h3 = repo.snapshot("third message: breaks everything")
    (project / "a.txt").unlink()                     # destructive command...
    (project / "junk.txt").write_text("leftover")    # ...and stray output

    snaps = repo.list_snapshots()
    assert [s.message for s in snaps] == [
        "first message", "second message", "third message: breaks everything"]

    # Revert to right before message 3: keeps turn 2's work, undoes turn 3.
    repo.revert_to(h3)
    assert (project / "a.txt").read_text() == "version 2"
    assert (project / "b.txt").exists()
    assert not (project / "junk.txt").exists()

    # Revert further back: before message 2.
    repo.revert_to(h2)
    assert (project / "a.txt").read_text() == "version 1"
    assert not (project / "b.txt").exists()


def test_changed_files_since_baseline(project):
    # Powers the voice mode "what did this worker change?" / "revert that" flow:
    # a baseline snapshot, then diff the work-tree against it.
    repo = backup.BackupRepo("sess-changes", project)
    (project / "a.txt").write_text("original")
    baseline = repo.snapshot("before worker")
    (project / "a.txt").write_text("edited")     # modify existing
    (project / "new.py").write_text("print(1)")  # add a new file

    changed = dict((path, st) for st, path in repo.changed_files_since(baseline))
    assert changed.get("new.py") == "A"
    assert changed.get("a.txt") == "M"
    diff = repo.diff_since(baseline)
    assert "new.py" in diff and "a.txt" in diff

    # And the baseline still works as a revert target.
    repo.revert_to(baseline)
    assert not (project / "new.py").exists()


def test_real_git_dir_survives_untouched(project):
    repo = backup.BackupRepo("sess-2", project)
    repo.snapshot("msg")
    (project / "x.txt").write_text("x")
    h = repo.snapshot("msg 2")
    (project / "x.txt").unlink()
    repo.revert_to(h)

    real_git = project / ".git"
    assert real_git.exists()
    assert (real_git / "config").read_text() == "THE USER'S REAL GIT CONFIG"
    assert (real_git / "objects" / "blob").exists()


def test_shadow_git_dir_lives_outside_project(project):
    repo = backup.BackupRepo("sess-3", project)
    repo.snapshot("msg")
    assert backup.BACKUPS_DIR in repo.git_dir.parents
    assert project not in repo.git_dir.parents


def test_default_excludes_skip_heavy_dirs(project):
    repo = backup.BackupRepo("sess-4", project)
    (project / "node_modules").mkdir()
    (project / "node_modules" / "big.js").write_text("x" * 1000)
    (project / "real.txt").write_text("keep me")
    h = repo.snapshot("msg")
    (project / "real.txt").unlink()
    shutil.rmtree(project / "node_modules")
    repo.revert_to(h)
    assert (project / "real.txt").exists()
    # node_modules was excluded from the snapshot -- revert must not
    # resurrect it.
    assert not (project / "node_modules").exists()


def test_run_suppresses_console_window(project, monkeypatch):
    """Every git call goes through subprocess.run under pythonw (no console
    of its own), so it must always carry NO_WINDOW_KWARGS -- otherwise each
    one flashes a black console window on Windows. Regression test for a
    real bug: snapshot() runs before EVERY turn, so a missing flag here
    means a window flash on every single message sent.

    NO_WINDOW_KWARGS is only non-empty on win32 (see tools.py), so on this
    (Linux) test runner it's normally {} and the flag wouldn't actually be
    exercised -- force a sentinel value so the assertion is meaningful on
    every platform CI runs on."""
    monkeypatch.setattr(backup, "NO_WINDOW_KWARGS", {"creationflags": 0x08000000})
    seen_kwargs = {}
    real_run = backup.subprocess.run

    def spy(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return real_run(*args, **{k: v for k, v in kwargs.items() if k != "creationflags"})

    monkeypatch.setattr(backup.subprocess, "run", spy)
    repo = backup.BackupRepo("sess-5", project)
    repo.snapshot("msg")
    assert seen_kwargs.get("creationflags") == 0x08000000


# --------------------------------------------------------------------- #
# revert_paths_to: undoing ONE background worker
#
# revert_to() below is the whole-tree instrument, and for undoing one worker it
# is the wrong one: `reset --hard` throws away everything done since that
# worker started -- another worker's work, and the user's own edits -- and
# `clean -fd` additionally deletes untracked files that were never part of any
# diff. Neither is what "revert this worker" means.

def test_it_restores_only_the_paths_it_is_given(project):
    repo = backup.BackupRepo("sess-paths", project)
    (project / "auth.py").write_text("original auth")
    (project / "other.py").write_text("original other")
    base = repo.snapshot("before the worker")

    (project / "auth.py").write_text("worker's edit")
    (project / "other.py").write_text("someone else's edit")

    done = repo.revert_paths_to(base, ["auth.py"])

    assert done == ["auth.py"]
    assert (project / "auth.py").read_text() == "original auth"
    assert (project / "other.py").read_text() == "someone else's edit"


def test_a_file_the_worker_created_is_removed(project):
    """Undoing the creation of a file is deleting it -- there is no earlier
    version to put back."""
    repo = backup.BackupRepo("sess-new", project)
    base = repo.snapshot("before")
    (project / "new.py").write_text("brand new")

    assert repo.revert_paths_to(base, ["new.py"]) == ["new.py"]
    assert not (project / "new.py").exists()


def test_a_file_the_worker_deleted_comes_back(project):
    repo = backup.BackupRepo("sess-del", project)
    (project / "gone.py").write_text("still here")
    base = repo.snapshot("before")
    (project / "gone.py").unlink()

    repo.revert_paths_to(base, ["gone.py"])
    assert (project / "gone.py").read_text() == "still here"


def test_untracked_files_it_was_not_asked_about_survive(project):
    """The reason this exists rather than reset --hard: `clean -fd` deletes
    every untracked file in the project, including ones no diff ever mentioned
    -- build output, scratch notes, a half-written file the user is editing."""
    repo = backup.BackupRepo("sess-clean", project)
    (project / "auth.py").write_text("original")
    base = repo.snapshot("before")
    (project / "auth.py").write_text("changed")
    (project / "scratch.txt").write_text("the user's own untracked note")

    repo.revert_paths_to(base, ["auth.py"])
    assert (project / "scratch.txt").read_text() == "the user's own untracked note"


def test_the_projects_real_git_is_never_touched(project):
    """The same rule the whole-tree revert follows."""
    repo = backup.BackupRepo("sess-git", project)
    base = repo.snapshot("before")
    (project / "a.txt").write_text("x")
    repo.revert_paths_to(base, ["a.txt", ".git/config"])
    assert (project / ".git" / "config").read_text() == "THE USER'S REAL GIT CONFIG"


def test_a_path_escaping_the_project_is_skipped(project, tmp_path):
    """These paths come from a diff this class produced -- but a check that
    only holds because of where the caller got its input is not a check."""
    outside = tmp_path / "outside.txt"
    outside.write_text("not yours")
    repo = backup.BackupRepo("sess-escape", project)
    base = repo.snapshot("before")

    assert repo.revert_paths_to(base, ["../outside.txt"]) == []
    assert outside.read_text() == "not yours"


def test_reverting_nothing_is_not_an_error(project):
    repo = backup.BackupRepo("sess-empty", project)
    base = repo.snapshot("before")
    assert repo.revert_paths_to(base, []) == []


def test_it_refuses_before_any_snapshot_exists(project):
    repo = backup.BackupRepo("sess-none", project)
    with pytest.raises(RuntimeError):
        repo.revert_paths_to("deadbeef", ["a.txt"])
