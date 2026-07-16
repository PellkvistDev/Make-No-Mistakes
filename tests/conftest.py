"""Shared fakes for the test suite.

The core principle: NO network, NO real API keys, NO timing-dependent
sleeps. Model behavior is scripted per-test via ScriptedClient, and the
agent reports into RecordingEvents instead of a real UI.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo root importable no matter where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from glmcode.events import AgentEvents  # noqa: E402


class FakeResult:
    """Stands in for api.ChatResult."""

    def __init__(self, tool_calls=None, content="", finish_reason=None,
                 prompt_tokens=1, completion_tokens=1):
        self.tool_calls = tool_calls or []
        self.content = content
        self.reasoning = ""
        self.finish_reason = finish_reason or ("tool_calls" if tool_calls else "stop")
        self.usage = _U(prompt_tokens, completion_tokens)

    def to_message(self):
        m = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            m["tool_calls"] = self.tool_calls
        return m


class _U:
    def __init__(self, p, c):
        self.prompt_tokens = p
        self.completion_tokens = c


def tool_call(call_id: str, name: str = "list_dir", arguments: str = "{}") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": arguments}}


class ScriptedClient:
    """A ZaiClient stand-in whose responses come from a per-instance script.

    `ScriptedClient.scripts` is a queue of callables; each newly constructed
    client pops one and uses it for every chat() call: script(call_number)
    must return a FakeResult or raise. Set the queue AFTER constructing any
    coordinator Agent (its own client pops from the queue too).
    """

    scripts: list = []

    def __init__(self, api_key="test-key", base_url="http://test", rate_limiter=None):
        self.api_key = api_key
        self.base_url = base_url
        self.rate_limiter = rate_limiter
        self.n = 0
        self._script = (ScriptedClient.scripts.pop(0) if ScriptedClient.scripts
                        else (lambda i: FakeResult(content="ok")))

    def chat(self, **kwargs):
        self.n += 1
        if kwargs.get("tools") is None:
            # A forced wrap-up call (tools withheld). Scripts may special-case
            # this by inspecting kwargs, but the common case is: answer.
            return FakeResult(content="(forced wrap-up report)")
        return self._script(self.n)


class RecordingEvents(AgentEvents):
    """Captures everything the agent reports, for assertions."""

    def __init__(self):
        self.content = []
        self.reasoning = []
        self.notices = []          # (level, msg)
        self.steered_texts = []
        self.steer_returned_texts = []
        self.wrapups = 0
        self.subagent_events = []  # (id, status, summary)
        self.streams = []          # (id, kind, data)

    def content_delta(self, text):
        self.content.append(text)

    def reasoning_delta(self, text):
        self.reasoning.append(text)

    def info(self, msg):
        self.notices.append(("info", msg))

    def warn(self, msg):
        self.notices.append(("warn", msg))

    def error(self, msg):
        self.notices.append(("error", msg))

    def steered(self, text):
        self.steered_texts.append(text)

    def steer_returned(self, text):
        self.steer_returned_texts.append(text)

    def wrapup_requested(self):
        self.wrapups += 1

    def subagent(self, id, name, status, mission="", summary=""):
        self.subagent_events.append((id, status, summary))

    def subagent_stream(self, id, kind, **data):
        self.streams.append((id, kind, data))


@pytest.fixture
def events():
    return RecordingEvents()


@pytest.fixture
def scripted_agent(monkeypatch, events):
    """An Agent wired to ScriptedClient + RecordingEvents. Returns a factory:
    call it with a script to get the agent (sub-agent spawning also patched)."""
    import glmcode.agent as agent_mod
    from glmcode.agent import Agent
    from glmcode.config import Config

    monkeypatch.setattr(agent_mod, "ZaiClient", ScriptedClient)
    ScriptedClient.scripts = []

    def make(script=None, allow_subagents=False):
        client = ScriptedClient()
        if script is not None:
            client._script = script
        return Agent(Config(), client, events=events, allow_subagents=allow_subagents)

    yield make
    ScriptedClient.scripts = []
