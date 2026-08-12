"""A tool call whose arguments aren't JSON must not wedge the chat.

Telling the model "could not parse tool arguments" is only half the job: the
broken call stays in the assistant message, and that message goes back out on
every following request. Google answers each one with

    API error 400: Request contains an invalid argument.

so the chat is finished -- every retry, every new question, forever, with no
way out but deleting it. The arguments are replaced in the stored history
instead (the raw text is already quoted in the tool reply, so nothing is
lost).
"""

import json

from glmcode.agent import Agent
from glmcode.config import Config

from conftest import FakeResult, ScriptedClient, tool_call


BAD = '{"path":"a.png"}{"path":"b.png"}'      # what Google actually sent


def _run(monkeypatch, events, arguments):
    import glmcode.agent as agent_mod
    monkeypatch.setattr(agent_mod, "ZaiClient", ScriptedClient)
    ScriptedClient.scripts = []
    client = ScriptedClient()
    client._script = lambda n: (
        FakeResult(tool_calls=[tool_call("c1", "list_dir", arguments)])
        if n == 1 else FakeResult(content="done"))
    agent = Agent(Config(), client, events=events)
    agent.run_turn({"role": "user", "content": "what do you think about the images"})
    return agent


def _tool_calls(agent):
    return [tc for m in agent.messages for tc in (m.get("tool_calls") or [])]


def test_the_history_left_behind_is_still_sendable(monkeypatch, events):
    agent = _run(monkeypatch, events, BAD)
    calls = _tool_calls(agent)
    assert calls, "the assistant turn should still be in the history"
    for tc in calls:
        json.loads(tc["function"]["arguments"])   # the 400, reproduced


def test_the_model_is_still_told_what_it_sent(monkeypatch, events):
    """Repairing the history must not cost the model the feedback it needs to
    do better on the retry -- the raw text moves into the tool reply, it does
    not disappear."""
    agent = _run(monkeypatch, events, BAD)
    replies = [m for m in agent.messages if m.get("role") == "tool"]
    assert replies and BAD in replies[0]["content"]
    assert "could not parse" in replies[0]["content"]


def test_arguments_that_parse_are_left_exactly_as_they_came(monkeypatch, events):
    agent = _run(monkeypatch, events, '{"path": "."}')
    assert _tool_calls(agent)[0]["function"]["arguments"] == '{"path": "."}'
