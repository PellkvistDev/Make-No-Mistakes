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
