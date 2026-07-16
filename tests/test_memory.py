"""Persistent user-level memory (the `remember` tool)."""

import pytest

import glmcode.prompts as prompts
import glmcode.tools as tools
from glmcode.errors import ToolError


@pytest.fixture
def tmp_memory(monkeypatch, tmp_path):
    mem = tmp_path / "memory.md"
    monkeypatch.setattr(tools, "MEMORY_FILE", mem)
    # prompts._user_memory reads the path from glmcode.config at call time
    import glmcode.config as config
    monkeypatch.setattr(config, "MEMORY_FILE", mem)
    return mem


def test_remember_appends_and_loads(tmp_memory):
    assert tools.load_memory() == ""
    tools.remember("Prefers 2-space indentation")
    tools.remember("Always run tests before saying done")
    text = tmp_memory.read_text(encoding="utf-8")
    assert "- Prefers 2-space indentation" in text
    assert "- Always run tests before saying done" in text
    loaded = tools.load_memory()
    assert "Prefers 2-space indentation" in loaded


def test_remember_rejects_empty(tmp_memory):
    with pytest.raises(ToolError):
        tools.remember("   ")


def test_memory_lands_in_system_prompt(tmp_memory, tmp_path):
    tools.remember("Only ever use tabs")
    sp = prompts.build_system_prompt(tmp_path, "test-model")
    assert "Things to remember about this user" in sp
    assert "Only ever use tabs" in sp
    assert str(tmp_memory) in sp  # the model is told WHERE the file lives


def test_no_memory_no_section(tmp_memory, tmp_path):
    sp = prompts.build_system_prompt(tmp_path, "test-model")
    assert "Things to remember about this user" not in sp


def test_remember_registered_everywhere():
    assert "remember" in tools.TOOL_FUNCTIONS
    assert "remember" in tools.READONLY_TOOLS  # no permission prompt
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    assert "remember" in names
