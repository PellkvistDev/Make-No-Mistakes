"""An undo you cannot undo is not a safety net.

Found by asking which failures are the expensive ones rather than which are the
visible ones. Every report this session has been about something you can see; a
UI papercut costs a minute, and losing an afternoon's work costs the project.

Two defects, both in the file whose whole job is not losing work:

  1. revert_to() is `reset --hard` plus `clean -fd`, and snapshots are taken
     BEFORE a user turn -- so everything the newest turn produced, and anything
     edited by hand since, was in no commit at all. Rewinding past it destroyed
     that permanently. Edit-and-resend a message from five turns ago and those
     five turns were gone for good.
  2. The path-containment guard was a string prefix. "/home/you/proj" prefixes
     "/home/you/proj-evil", so a sibling directory satisfied the check -- on
     the two functions here whose job includes deleting files. The comment
     beside one of them already said "a check that only holds because of where
     the caller got its input is not a check"; it just was not true of itself.
"""

import subprocess

import pytest

from glmcode import backup as B


needs_git = pytest.mark.skipif(not B.available(), reason="git unavailable")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # The shadow repo goes under tmp_path, like tests/test_backup.py does --
    # these tests delete files, and they must never reach a real one.
    monkeypatch.setattr(B, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "kept.txt").write_text("original\n")
    r = B.BackupRepo("sess-undo", proj)
    r.ensure_init()
    return r, proj


# ------------------------------------------------- the undo is undoable --

@needs_git
def test_work_since_the_last_snapshot_is_kept_before_it_is_destroyed(repo):
    """The whole finding. This work is in NO commit -- it exists on disk and
    nowhere else -- and reverting used to delete it outright."""
    r, proj = repo
    base = r.snapshot("turn 1")
    (proj / "kept.txt").write_text("edited by the last turn\n")
    (proj / "new.txt").write_text("made by the last turn\n")

    r.revert_to(base)
    assert (proj / "kept.txt").read_text() == "original\n"
    assert not (proj / "new.txt").exists()

    # ...and it is recoverable, which is the part that was missing.
    safety = [s for s in r.list_snapshots() if s.message == B.REVERT_SAFETY_LABEL]
    assert safety, "the destroyed state was not kept anywhere"
    r.revert_to(safety[-1].commit)
    assert (proj / "kept.txt").read_text() == "edited by the last turn\n"
    assert (proj / "new.txt").read_text() == "made by the last turn\n"


@needs_git
def test_the_safety_point_is_named_so_it_can_be_found(repo):
    """It sits in Settings' restore-point list beside the per-turn ones, and
    "before this undo" is exactly the point you want after undoing too far."""
    r, proj = repo
    base = r.snapshot("turn 1")
    (proj / "kept.txt").write_text("changed\n")
    r.revert_to(base)
    msgs = [s.message for s in r.list_snapshots()]
    assert B.REVERT_SAFETY_LABEL in msgs
    assert "undo" in B.REVERT_SAFETY_LABEL


@needs_git
def test_reverting_still_actually_reverts(repo):
    """The safety copy must not quietly turn the undo into a no-op."""
    r, proj = repo
    base = r.snapshot("turn 1")
    (proj / "kept.txt").write_text("changed\n")
    r.revert_to(base)
    assert (proj / "kept.txt").read_text() == "original\n"


@needs_git
def test_a_failed_safety_snapshot_does_not_block_the_undo(repo, monkeypatch):
    """Best-effort: the user asked to undo, and refusing because we could not
    keep a copy would be the wrong trade."""
    r, proj = repo
    base = r.snapshot("turn 1")
    (proj / "kept.txt").write_text("changed\n")
    monkeypatch.setattr(r, "snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    r.revert_to(base)
    assert (proj / "kept.txt").read_text() == "original\n"


# ------------------------------------------------- containment -----------

@needs_git
def test_a_sibling_directory_is_not_inside_the_project(repo):
    """"/home/you/proj" prefixes "/home/you/proj-evil". The prefix form let
    that through on a function that deletes files."""
    r, proj = repo
    sibling = proj.parent / "proj-evil"
    sibling.mkdir()
    victim = sibling / "secrets.env"
    victim.write_text("do not delete me\n")

    with pytest.raises(RuntimeError, match="escapes"):
        r.revert_file("../proj-evil/secrets.env")
    assert victim.exists()


@needs_git
def test_the_bulk_revert_refuses_the_same_path(repo):
    """revert_paths_to takes its list from a diff this class produced -- but a
    check that only holds because of where the caller got its input is not a
    check, which is what its own comment says."""
    r, proj = repo
    base = r.snapshot("turn 1")
    sibling = proj.parent / "proj-evil"
    sibling.mkdir()
    victim = sibling / "secrets.env"
    victim.write_text("do not delete me\n")

    done = r.revert_paths_to(base, ["../proj-evil/secrets.env"])
    assert done == []
    assert victim.exists()


@needs_git
def test_an_ordinary_path_inside_the_project_still_works(repo):
    """The guard must not have been tightened into uselessness."""
    r, proj = repo
    r.snapshot("turn 1")
    (proj / "kept.txt").write_text("changed\n")
    r.revert_file("kept.txt")
    assert (proj / "kept.txt").read_text() == "original\n"
