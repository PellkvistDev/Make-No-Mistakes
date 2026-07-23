"""'Make it green': the opt-in, bounded test-fix loop. It only engages when
enabled AND the turn edited files AND a check command exists AND the tests
fail -- and it stops (giving up with an honest report) after a hard cap. Never
loops on a small / no-test / passing task."""

import json
import os

import pytest

from glmcode.agent import GREEN_LOOP_MAX_ROUNDS

from conftest import FakeResult, tool_call

GREEN_PREFIX = "[Automatic test run -- not from the user]"


def _edit_then_answer(tmp_path):
    def script(n):
        if n == 1:
            args = json.dumps({"path": str(tmp_path / "f.py"), "content": "x = 1\n"})
            return FakeResult([tool_call("c1", "write_file", args)])
        return FakeResult(content="done")
    return script


def _green_msgs(agent):
    return [m for m in agent.messages
            if (m.get("content") or "").startswith(GREEN_PREFIX)]


def test_loop_fixes_until_tests_pass(scripted_agent, tmp_path, monkeypatch, events):
    (tmp_path / "tests").mkdir()                 # makes detect_check_command -> pytest
    agent = scripted_agent(_edit_then_answer(tmp_path), allow_subagents=True)
    agent.cfg.auto_fix_tests = True
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    checks = iter([(False, "FAILED boom"), (True, "ok")])
    monkeypatch.setattr(agent, "_run_check", lambda cmd: next(checks))

    agent.run_turn({"role": "user", "content": "edit"})

    green = _green_msgs(agent)
    assert len(green) == 1                       # one fix nudge, then green -> stop
    assert "FAILED boom" in green[0]["content"]
    assert any("fixing" in msg for lvl, msg in events.notices)


def test_loop_gives_up_after_cap(scripted_agent, tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    agent = scripted_agent(_edit_then_answer(tmp_path), allow_subagents=True)
    agent.cfg.auto_fix_tests = True
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    monkeypatch.setattr(agent, "_run_check", lambda cmd: (False, "still red"))

    agent.run_turn({"role": "user", "content": "edit"})

    green = _green_msgs(agent)
    fixes = [m for m in green if "ROOT CAUSE" in m["content"]]
    giveups = [m for m in green if "Stop trying to fix" in m["content"]]
    assert len(fixes) == GREEN_LOOP_MAX_ROUNDS   # bounded -- never infinite
    assert len(giveups) == 1


def test_no_loop_when_disabled(scripted_agent, tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    agent = scripted_agent(_edit_then_answer(tmp_path), allow_subagents=True)
    agent.cfg.auto_fix_tests = False             # off by default
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    called = {"n": 0}
    monkeypatch.setattr(agent, "_run_check",
                        lambda cmd: called.__setitem__("n", called["n"] + 1) or (False, "x"))
    agent.run_turn({"role": "user", "content": "edit"})
    assert _green_msgs(agent) == [] and called["n"] == 0


def test_no_loop_without_edits(scripted_agent, tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    agent = scripted_agent(lambda n: FakeResult(content="just an answer"),
                           allow_subagents=True)
    agent.cfg.auto_fix_tests = True
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    monkeypatch.setattr(agent, "_run_check", lambda cmd: (False, "x"))
    agent.run_turn({"role": "user", "content": "just a question"})
    assert _green_msgs(agent) == []              # nothing was edited -> nothing to verify


def test_no_loop_when_no_tests_detected(scripted_agent, tmp_path, monkeypatch):
    # No tests/ dir, no package.json etc. -> detect_check_command returns "",
    # so the check command is never even run.
    agent = scripted_agent(_edit_then_answer(tmp_path), allow_subagents=True)
    agent.cfg.auto_fix_tests = True
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    def boom(cmd):
        raise AssertionError("_run_check must not be called when no tests exist")
    monkeypatch.setattr(agent, "_run_check", boom)
    agent.run_turn({"role": "user", "content": "edit"})
    assert _green_msgs(agent) == []


def test_subagents_never_run_the_loop(scripted_agent, tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    agent = scripted_agent(_edit_then_answer(tmp_path), allow_subagents=False)  # a sub-agent
    agent.cfg.auto_fix_tests = True
    agent.workdir = tmp_path
    agent.permissions.mode = "yolo"
    monkeypatch.setattr(agent, "_run_check", lambda cmd: (False, "x"))
    agent.run_turn({"role": "user", "content": "edit"})
    assert _green_msgs(agent) == []


# --------------------------------------------------- the real command runner --

@pytest.mark.skipif(os.name == "nt", reason="uses /bin/sh; PowerShell path covered on Windows")
def test_run_check_command_reports_exit_code(tmp_path):
    import glmcode.tools as tools
    tools.set_workdir(tmp_path)
    code, out = tools.run_check_command("echo hello")
    assert code == 0 and "hello" in out
    code, _ = tools.run_check_command("exit 3")
    assert code == 3
