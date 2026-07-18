"""MCP client: stdio handshake, tool discovery, calls, agent integration,
and resilience to broken servers."""

import sys
from pathlib import Path

import pytest

from glmcode.config import Config
from glmcode.mcp import McpManager, McpServer, _sanitize
from glmcode.errors import ToolError as ToolErrorBase

from conftest import FakeResult, tool_call

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"
FAKE_CMD = f'"{sys.executable}" "{FAKE_SERVER}"'


@pytest.fixture
def manager():
    cfg = Config(mcp_servers=[{"name": "fake", "command": FAKE_CMD}])
    m = McpManager(cfg)
    m.start_all()
    yield m
    m.stop_all()


def test_handshake_discovers_tools(manager):
    st = manager.status()
    assert len(st) == 1 and st[0]["running"] is True
    assert st[0]["tools"] == ["mcp_fake_echo"]
    schemas = manager.tool_schemas()
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "mcp_fake_echo"
    assert "fake MCP server" in fn["description"]
    assert fn["parameters"]["properties"]["text"]["type"] == "string"


def test_call_roundtrip(manager):
    assert manager.owns("mcp_fake_echo")
    assert not manager.owns("read_file")
    out = manager.call("mcp_fake_echo", {"text": "hello mcp"})
    assert out == "HELLO MCP"


def test_unknown_tool_and_dead_server_raise(manager):
    with pytest.raises(ToolErrorBase):
        manager.call("mcp_fake_nope", {})
    manager.stop_all()
    with pytest.raises(ToolErrorBase):
        manager.call("mcp_fake_echo", {"text": "x"})


def test_broken_command_reports_error_not_crash():
    cfg = Config(mcp_servers=[{"name": "bad",
                               "command": f'"{sys.executable}" -c "raise SystemExit(1)"'}])
    m = McpManager(cfg)
    m.start_all()  # must not raise
    st = m.status()
    assert st[0]["running"] is False
    assert m.tool_schemas() == []  # no tools from a dead server
    m.stop_all()


def test_agent_dispatches_mcp_tool(scripted_agent, manager):
    calls = iter([
        FakeResult(tool_calls=[tool_call("m1", "mcp_fake_echo",
                                         '{"text": "from the model"}')]),
        FakeResult(content="done"),
    ])
    agent = scripted_agent(lambda n: next(calls))
    agent.set_mode("yolo")   # MCP tools are permission-gated like any other
    agent.mcp = manager

    seen_tools = {}
    orig = agent.client.chat

    def spy(**kw):
        seen_tools["names"] = [t["function"]["name"] for t in (kw.get("tools") or [])]
        return orig(**kw)

    agent.client.chat = spy
    agent.run_turn({"role": "user", "content": "use the mcp tool"})

    # the MCP tool was offered to the model...
    assert "mcp_fake_echo" in seen_tools["names"]
    # ...and its result landed in the conversation as a tool reply
    tool_replies = [m for m in agent.messages if m.get("role") == "tool"]
    assert any(m["content"] == "FROM THE MODEL" for m in tool_replies)


def test_sanitize():
    assert _sanitize("my server!") == "my_server_"
    assert _sanitize("a" * 100) == "a" * 40
