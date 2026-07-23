"""Fresh-eyes review: when a High/Max turn produced a real diff, the review
pass is an INDEPENDENT critic that sees only the task + diff (not the agent's
reasoning). It approves (stop) or feeds concrete findings back to fix. With no
change tracking / no diff, it falls back to the in-context self-review."""

import pytest

from glmcode.backup import BackupRepo, available
from glmcode.prompts import (FRESH_REVIEW_HEADER, REFINE_NUDGE,
                             blind_critique_prompt, fresh_review_nudge,
                             is_critic_approval)

from conftest import FakeResult, tool_call

needs_git = pytest.mark.skipif(not available(), reason="git not installed")


# ---------------------------------------------------------- pure helpers --

def test_is_critic_approval_strict():
    assert is_critic_approval("APPROVED")
    assert is_critic_approval("approved.")
    assert is_critic_approval("  APPROVED!  ")
    # Any hedge is NOT approval, so the notes still reach the agent.
    assert not is_critic_approval("APPROVED, but check the null case")
    assert not is_critic_approval("Looks good")
    assert not is_critic_approval("")


def test_blind_critique_prompt_carries_task_and_diff():
    p = blind_critique_prompt("add a flag", "-old\n+new")
    assert "add a flag" in p and "+new" in p


def test_fresh_review_nudge_wraps_findings():
    n = fresh_review_nudge("problem: off-by-one in loop")
    assert n.startswith(FRESH_REVIEW_HEADER)
    assert "off-by-one" in n


# --------------------------------------------------- _refine_nudge routing --

def _high(agent):
    agent.cfg.thinking_mode = "high"
    agent.allow_subagents = True
    return agent


def test_refine_falls_back_to_self_review_without_backup(scripted_agent):
    agent = _high(scripted_agent())            # backup_repo is None
    agent._turn_task = "do a thing"
    assert agent._refine_nudge() == REFINE_NUDGE


@needs_git
def test_refine_falls_back_when_no_diff(scripted_agent, tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    agent = _high(scripted_agent())
    agent._turn_task = "look at it"
    agent.backup_repo = BackupRepo("sess", proj)
    agent.backup_repo.snapshot("pre-turn")           # no edits after -> empty diff
    # No diff to judge -> the in-context self-review, not the blind critic.
    called = {"critic": False}
    monkeypatch.setattr(agent, "_blind_critique",
                        lambda *a: called.__setitem__("critic", True) or "APPROVED")
    assert agent._refine_nudge() == REFINE_NUDGE
    assert called["critic"] is False


@needs_git
def test_refine_uses_blind_critic_on_real_diff(scripted_agent, tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    agent = _high(scripted_agent())
    agent._turn_task = "bump x"
    agent.backup_repo = BackupRepo("sess", proj)
    agent.backup_repo.snapshot("pre-turn")
    (proj / "a.py").write_text("x = 2\n", encoding="utf-8")   # a real change

    seen = {}
    def fake_critic(task, diff):
        seen["task"], seen["diff"] = task, diff
        return "problem: forgot to update the test"
    monkeypatch.setattr(agent, "_blind_critique", fake_critic)

    nudge = agent._refine_nudge()
    assert nudge is not None and nudge.startswith(FRESH_REVIEW_HEADER)
    assert "forgot to update the test" in nudge
    assert seen["task"] == "bump x"
    assert "+x = 2" in seen["diff"]          # the critic really saw the diff


@needs_git
def test_refine_approves_and_stops(scripted_agent, tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    agent = _high(scripted_agent())
    agent._turn_task = "bump x"
    agent.backup_repo = BackupRepo("sess", proj)
    agent.backup_repo.snapshot("pre-turn")
    (proj / "a.py").write_text("x = 2\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_blind_critique", lambda *a: "APPROVED")
    assert agent._refine_nudge() is None      # nothing to fix -> stop refining


@needs_git
def test_refine_falls_back_when_critic_unavailable(scripted_agent, tmp_path, monkeypatch):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.py").write_text("x = 1\n", encoding="utf-8")
    agent = _high(scripted_agent())
    agent._turn_task = "bump x"
    agent.backup_repo = BackupRepo("sess", proj)
    agent.backup_repo.snapshot("pre-turn")
    (proj / "a.py").write_text("x = 2\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_blind_critique", lambda *a: "")   # call failed
    assert agent._refine_nudge() == REFINE_NUDGE


# --------------------------------------------------------- turn integration --

@needs_git
def test_high_turn_injects_independent_review(scripted_agent, tmp_path, monkeypatch):
    """End-to-end: an edit turn in High mode gets a blind-critic finding fed
    back as a user nudge that the agent then acts on."""
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("before\n", encoding="utf-8")

    def script(n):
        if n == 1:                          # edit, then answer
            (tmp_path / "f.txt").write_text("after\n", encoding="utf-8")
            return FakeResult(content="done")
        return FakeResult(content="fixed the finding")

    agent = _high(scripted_agent(script))
    agent.backup_repo = BackupRepo("sess", tmp_path)
    agent.backup_repo.snapshot("pre-turn")
    monkeypatch.setattr(agent, "_blind_critique", lambda *a: "problem: missing edge case")
    agent.run_turn({"role": "user", "content": "change the file"})

    reviews = [m for m in agent.messages
               if m.get("role") == "user" and (m.get("content") or "").startswith(FRESH_REVIEW_HEADER)]
    assert len(reviews) == 1 and "missing edge case" in reviews[0]["content"]
