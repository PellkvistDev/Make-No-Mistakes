"""review_changes tool (shadow-git turn diffs) + the verify-after-edit nudge
+ history-replay filtering of internal nudge messages."""

import json
import shutil

import pytest

from glmcode.backup import BackupRepo, available
from glmcode.prompts import STEER_NUDGE_TEMPLATE, VERIFY_NUDGE
from glmcode.sessions import to_display

from conftest import FakeResult, tool_call

needs_git = pytest.mark.skipif(not available(), reason="git not installed")


# ------------------------------------------------------------- turn_diff --

@needs_git
def test_turn_diff_shows_edits_and_new_files(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo = BackupRepo("sess", proj)
    repo.snapshot("turn starts")

    (proj / "a.py").write_text("x = 2\n", encoding="utf-8")   # edit
    (proj / "new.py").write_text("y = 3\n", encoding="utf-8")  # new file

    diff = repo.turn_diff()
    assert "a.py" in diff and "new.py" in diff
    assert "-x = 1" in diff and "+x = 2" in diff
    assert "+y = 3" in diff  # untracked files must appear too


@needs_git
def test_turn_diff_no_changes(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    repo = BackupRepo("sess", proj)
    repo.snapshot("turn starts")
    assert "No changes" in repo.turn_diff()


def test_turn_diff_uninitialized(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    repo = BackupRepo("sess", tmp_path)
    assert "no pre-turn snapshot" in repo.turn_diff()


# ---------------------------------------------------------- turn_changes --

@needs_git
def test_turn_changes_structured(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "gone.py").write_text("bye\n", encoding="utf-8")
    repo = BackupRepo("sess", proj)
    repo.snapshot("turn starts")

    (proj / "mod.py").write_text("x = 2\n", encoding="utf-8")
    (proj / "new.py").write_text("hello\n", encoding="utf-8")
    (proj / "gone.py").unlink()

    files = {f["path"]: f for f in repo.turn_changes()}
    assert files["mod.py"]["status"] == "M" and "+x = 2" in files["mod.py"]["diff"]
    assert files["new.py"]["status"] == "A" and "+hello" in files["new.py"]["diff"]
    assert files["gone.py"]["status"] == "D"


@needs_git
def test_revert_file_each_status(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "mod.py").write_text("original\n", encoding="utf-8")
    (proj / "gone.py").write_text("keep me\n", encoding="utf-8")
    repo = BackupRepo("sess", proj)
    repo.snapshot("turn starts")

    (proj / "mod.py").write_text("clobbered\n", encoding="utf-8")
    (proj / "new.py").write_text("junk\n", encoding="utf-8")
    (proj / "gone.py").unlink()

    repo.revert_file("mod.py")
    assert (proj / "mod.py").read_text(encoding="utf-8") == "original\n"
    repo.revert_file("new.py")
    assert not (proj / "new.py").exists()  # added -> reverting deletes it
    repo.revert_file("gone.py")
    assert (proj / "gone.py").read_text(encoding="utf-8") == "keep me\n"
    # ...and only those files were touched; nothing else changed
    assert repo.turn_changes() == []


@needs_git
def test_revert_file_blocks_path_escape(tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("precious", encoding="utf-8")
    repo = BackupRepo("sess", proj)
    repo.snapshot("s")
    with pytest.raises(RuntimeError):
        repo.revert_file("../outside.txt")
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "precious"


# ------------------------------------------------- review_changes dispatch --

@needs_git
def test_review_changes_tool_reaches_backup_repo(scripted_agent, tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("before\n", encoding="utf-8")

    def script(n):
        if n == 1:
            (tmp_path / "f.txt").write_text("after\n", encoding="utf-8")
            return FakeResult([tool_call("c1", "review_changes")])
        return FakeResult(content="reviewed")

    agent = scripted_agent(script)
    agent.backup_repo = BackupRepo("sess", tmp_path)
    agent.backup_repo.snapshot("pre-turn")
    agent.run_turn({"role": "user", "content": "check your work"})

    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("-before" in m["content"] and "+after" in m["content"] for m in tool_msgs)


def test_review_changes_without_repo_is_clear_error(scripted_agent):
    def script(n):
        if n == 1:
            return FakeResult([tool_call("c1", "review_changes")])
        return FakeResult(content="ok then")

    agent = scripted_agent(script)  # backup_repo stays None
    agent.run_turn({"role": "user", "content": "diff please"})
    tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("ERROR" in m["content"] and "change tracking" in m["content"]
               for m in tool_msgs)


# ------------------------------------------------------------ verify nudge --

def _yolo(agent):
    agent.permissions.mode = "yolo"
    return agent


def test_edit_without_verification_gets_one_nudge(scripted_agent, tmp_path, monkeypatch, events):
    monkeypatch.chdir(tmp_path)

    def script(n):
        if n == 1:
            args = json.dumps({"path": str(tmp_path / "f.py"), "content": "x = 1\n"})
            return FakeResult([tool_call("c1", "write_file", args)])
        return FakeResult(content="done (no verification run)")

    agent = _yolo(scripted_agent(script))
    agent.cfg.verify_edits = True   # opt-in (off by default)
    agent.run_turn({"role": "user", "content": "make the file"})

    nudges = [m for m in agent.messages if m.get("content") == VERIFY_NUDGE]
    assert len(nudges) == 1
    assert agent.client.n == 3  # edit round, nudged answer, final answer
    assert any("verify" in msg for lvl, msg in events.notices if lvl == "info")


def test_no_nudge_when_verify_edits_disabled(scripted_agent, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def script(n):
        if n == 1:
            args = json.dumps({"path": str(tmp_path / "f.py"), "content": "x = 1\n"})
            return FakeResult([tool_call("c1", "write_file", args)])
        return FakeResult(content="done (no verification, and that's fine)")

    agent = _yolo(scripted_agent(script))
    agent.cfg.verify_edits = False   # user just wants edits, no auto-run
    agent.run_turn({"role": "user", "content": "make the file"})
    assert not any(m.get("content") == VERIFY_NUDGE for m in agent.messages)
    assert agent.client.n == 2       # edit round + final answer, no nudge round


def test_no_nudge_when_turn_verified(scripted_agent, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def script(n):
        if n == 1:
            args = json.dumps({"path": str(tmp_path / "f.py"), "content": "x = 1\n"})
            return FakeResult([tool_call("c1", "write_file", args)])
        if n == 2:
            args = json.dumps({"command": "echo checked"})
            return FakeResult([tool_call("c2", "run_powershell", args)])
        return FakeResult(content="done, verified")

    agent = _yolo(scripted_agent(script))
    agent.run_turn({"role": "user", "content": "make and check"})
    assert not any(m.get("content") == VERIFY_NUDGE for m in agent.messages)


def test_no_nudge_without_edits(scripted_agent):
    agent = _yolo(scripted_agent(lambda n: FakeResult(content="just an answer")))
    agent.run_turn({"role": "user", "content": "question"})
    assert not any(m.get("content") == VERIFY_NUDGE for m in agent.messages)


# ------------------------------------------------------- history filtering --

def test_internal_nudges_hidden_from_history():
    msgs = [
        {"role": "user", "content": "real question"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": VERIFY_NUDGE},
        {"role": "assistant", "content": "verified, all good"},
        {"role": "user", "content": STEER_NUDGE_TEMPLATE.format(text="focus on tests")},
        {"role": "assistant", "content": "will do"},
    ]
    items = to_display(msgs)
    kinds = [(it["kind"], it.get("text", "")) for it in items]
    users = [t for k, t in kinds if k == "user"]
    assert users == ["real question"]           # nudge not shown as a user bubble
    steered = [t for k, t in kinds if k == "steered"]
    assert steered == ["focus on tests"]        # steering shown as its note, unframed
