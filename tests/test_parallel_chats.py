"""Parallel-chat safety: agents in different folders, running concurrently
on different threads, must never contaminate each other's file operations
through process-global state."""

import json
import threading

from conftest import FakeResult, tool_call


def test_concurrent_agents_write_into_their_own_workdirs(scripted_agent, tmp_path):
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    gate = threading.Barrier(2, timeout=10)

    def make_script(marker):
        def script(n):
            if n == 1:
                # Both agents pause here so their tool rounds interleave for
                # real -- then each writes a RELATIVE path.
                gate.wait()
                args = json.dumps({"path": "out.txt", "content": marker})
                return FakeResult([tool_call("c1", "write_file", args)])
            return FakeResult(content="done")
        return script

    agent_a = scripted_agent(make_script("from A"))
    agent_b = scripted_agent(make_script("from B"))
    agent_a.workdir = proj_a
    agent_b.workdir = proj_b
    agent_a.permissions.mode = "yolo"
    agent_b.permissions.mode = "yolo"

    ta = threading.Thread(target=agent_a.run_turn,
                          args=({"role": "user", "content": "write A"},))
    tb = threading.Thread(target=agent_b.run_turn,
                          args=({"role": "user", "content": "write B"},))
    ta.start(); tb.start()
    ta.join(timeout=20); tb.join(timeout=20)
    assert not ta.is_alive() and not tb.is_alive()

    assert (proj_a / "out.txt").read_text(encoding="utf-8") == "from A"
    assert (proj_b / "out.txt").read_text(encoding="utf-8") == "from B"


def test_subagent_inherits_coordinator_workdir(scripted_agent, tmp_path):
    from conftest import ScriptedClient
    proj = tmp_path / "proj"
    proj.mkdir()
    coord = scripted_agent(allow_subagents=True)
    coord.workdir = proj
    coord.cfg.mode = "yolo"  # sub-agents build their permission engine from cfg

    def sub_script(n):
        if n == 1:
            args = json.dumps({"path": "sub_out.txt", "content": "sub wrote this"})
            return FakeResult([tool_call("c1", "write_file", args)])
        return FakeResult(content="report")

    ScriptedClient.scripts = [sub_script]
    coord._run_subagents([{"name": "w", "task": "write the file"}])
    assert (proj / "sub_out.txt").read_text(encoding="utf-8") == "sub wrote this"
