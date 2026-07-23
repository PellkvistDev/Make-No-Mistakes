"""#2 Parallel attempts ("race", best-of-N): run several isolated attempts from
a common baseline and keep the best. Winner selection is unit-tested; the full
orchestration (revert between attempts, snapshot each, restore the winner's
files) runs against a real shadow-git BackupRepo with scripted attempt agents."""

import json

import pytest

from glmcode.agent import Agent
from glmcode.backup import BackupRepo, available

from conftest import FakeResult, tool_call

needs_git = pytest.mark.skipif(not available(), reason="git not installed")


# ------------------------------------------------------------- selection --

def test_pick_winner_prefers_passing_then_earliest():
    r = [
        {"attempt": 1, "commit": "a", "passed": False, "changed": True},
        {"attempt": 2, "commit": "b", "passed": True, "changed": True},
        {"attempt": 3, "commit": "c", "passed": True, "changed": True},
    ]
    assert Agent._pick_winner(r)["attempt"] == 2   # first passing


def test_pick_winner_unknown_beats_failing():
    r = [
        {"attempt": 1, "commit": "a", "passed": False, "changed": True},
        {"attempt": 2, "commit": "b", "passed": None, "changed": True},
    ]
    assert Agent._pick_winner(r)["attempt"] == 2


def test_pick_winner_change_beats_no_change():
    r = [
        {"attempt": 1, "commit": "a", "passed": None, "changed": False},
        {"attempt": 2, "commit": "b", "passed": None, "changed": True},
    ]
    assert Agent._pick_winner(r)["attempt"] == 2


# ----------------------------------------------------------- gating --

def test_race_off_by_default(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    assert agent.cfg.parallel_attempts == 1
    assert agent._should_race() is False


def test_race_needs_backup_repo(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    agent.cfg.parallel_attempts = 3
    assert agent._should_race() is False        # no backup_repo attached


def test_subagents_never_race(scripted_agent):
    agent = scripted_agent(allow_subagents=False)
    agent.cfg.parallel_attempts = 3
    assert agent._should_race() is False


# ---------------------------------------------------- full orchestration --

@needs_git
def test_race_runs_attempts_and_keeps_the_winner(scripted_agent, tmp_path, monkeypatch, events):
    import glmcode.backup as backup
    monkeypatch.setattr(backup, "BACKUPS_DIR", tmp_path / "shadow")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "base.txt").write_text("base\n", encoding="utf-8")

    agent = scripted_agent(allow_subagents=True)
    agent.cfg.parallel_attempts = 3
    agent.workdir = proj
    agent.permissions.mode = "yolo"
    agent.backup_repo = BackupRepo("sess", proj)

    # Each attempt writes a distinct file; only attempt 2 "passes" its tests.
    calls = {"n": 0}
    def fake_attempt(aid):
        calls["n"] += 1
        k = calls["n"]
        sub = type(agent)(agent.cfg, agent.client, events=events, allow_subagents=False,
                          workdir=proj)
        sub.backup_repo = agent.backup_repo
        def script(_):
            (proj / f"attempt{k}.txt").write_text(f"work {k}\n", encoding="utf-8")
            return FakeResult(content=f"did attempt {k}")
        sub.client._script = script
        sub.permissions.mode = "yolo"
        return sub
    monkeypatch.setattr(agent, "_make_attempt_agent", fake_attempt)
    scores = iter([(False, "fail"), (True, "pass"), (False, "fail")])
    monkeypatch.setattr(agent, "_score_attempt", lambda: next(scores))

    agent.run_turn({"role": "user", "content": "do the thing"})

    # The work-tree holds the WINNER's file (attempt 2) and not the losers'.
    assert (proj / "attempt2.txt").exists()
    assert not (proj / "attempt1.txt").exists()
    assert not (proj / "attempt3.txt").exists()
    assert (proj / "base.txt").exists()          # baseline preserved
    # The conversation ends with a summary naming the kept attempt.
    final = agent.messages[-1]
    assert final["role"] == "assistant" and "attempt 2" in final["content"].lower()


@needs_git
def test_race_falls_back_to_single_turn_without_git(scripted_agent, tmp_path, monkeypatch):
    # backup_repo present but snapshot returns None (git unavailable) -> one turn.
    proj = tmp_path / "p"
    proj.mkdir()
    agent = scripted_agent(lambda n: FakeResult(content="single"), allow_subagents=True)
    agent.cfg.parallel_attempts = 3
    agent.workdir = proj
    agent.backup_repo = BackupRepo("sess", proj)
    monkeypatch.setattr(agent.backup_repo, "snapshot", lambda *a, **k: None)
    # _should_race is True, but the None baseline makes it run a normal turn.
    agent.run_turn({"role": "user", "content": "hi"})
    assert any(m.get("content") == "single" for m in agent.messages)
