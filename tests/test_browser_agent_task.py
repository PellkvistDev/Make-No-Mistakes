"""The Browser Agent is told what its goal is.

Reported as "the browser agents are still not receiving their task", after the
sub-agent version of this was fixed. It was a different bug with the same
symptom, and it was not a Gemini quirk -- it was broken on every provider.

Two faults, stacked:

  1. `sub._base_system_prompt = BROWSER_AGENT_SYSTEM.format(goal=goal)` looked
     like it set the prompt and did not. Agent.__init__ has already run
     rebuild_system_prompt(), so messages[0] is written before any caller can
     touch that attribute -- and messages[0] is what goes on the wire. The
     browser agent ran with the CODING agent's system prompt.
  2. Even had it worked, the goal lived only in the system prompt. The user
     turn said "Begin. Work toward the goal", so the conversation contained no
     goal at all -- and a model that reads system content as background rather
     than as the request has nothing to do but ask what the task is.

So this checks what reaches the model, not what the code appears to set.
"""

import sys

import pytest

sys.path.insert(0, "tests")

from conftest import FakeResult, ScriptedClient  # noqa: E402

from glmcode.agent import Agent  # noqa: E402
from glmcode.config import Config  # noqa: E402
from glmcode.events import AgentEvents  # noqa: E402

GOAL = "Log into example.com and download the invoice for March"


class _Session:
    is_open = True

    def ensure_open(self):
        return None


@pytest.fixture
def browser(monkeypatch):
    """A coordinator that will spawn a Browser Agent, and the requests it makes."""
    import glmcode.agent as agent_mod
    sent = []

    class Spy(ScriptedClient):
        def chat(self, **kw):
            sent.append(kw)
            return FakeResult(content="I did the thing.")

    monkeypatch.setattr(agent_mod, "ZaiClient", Spy)
    ScriptedClient.scripts = []
    agent = Agent(Config(), Spy(), events=AgentEvents(), allow_subagents=True)
    agent.set_mode("yolo")
    agent.browser_session = _Session()
    yield agent, sent
    ScriptedClient.scripts = []


def _first_request(sent):
    assert sent, "the browser agent never called the model"
    return sent[-1]


# ------------------------------------------------- it knows what to do ----

def test_the_goal_reaches_the_conversation(browser):
    """The bug, stated plainly. "Begin. Work toward the goal" was the whole of
    what the model was asked."""
    agent, sent = browser
    agent._control_chrome_tool(GOAL)

    messages = _first_request(sent)["messages"]
    assert messages[-1]["role"] == "user"
    assert GOAL in messages[-1]["content"], \
        "the goal is not in the turn the model is answering"


def test_the_request_still_ends_on_the_turn(browser):
    """Same rule as everywhere else: a trailing system message is answered
    instead of the task above it."""
    agent, sent = browser
    agent._control_chrome_tool(GOAL)
    assert _first_request(sent)["messages"][-1]["role"] == "user"


# ------------------------------------- and it knows what it is supposed --

def test_it_gets_the_browser_prompt_not_the_coding_one(browser):
    """Assigning _base_system_prompt after construction updated a cache that
    messages[0] had already been built from. The agent driving a browser was
    being told it was an interactive coding agent."""
    agent, sent = browser
    agent._control_chrome_tool(GOAL)

    system = _first_request(sent)["messages"][0]["content"]
    assert system.startswith("You are the Browser Agent")
    assert "interactive coding agent" not in system


def test_the_goal_is_in_the_prompt_as_well(browser):
    """Belt and braces, and it is where the prompt's own instructions refer to
    it -- but it must not be the ONLY place."""
    agent, sent = browser
    agent._control_chrome_tool(GOAL)
    assert GOAL in _first_request(sent)["messages"][0]["content"]


def test_it_is_given_the_browser_tools_only(browser):
    """A browser agent handed edit_file would start writing code instead."""
    agent, sent = browser
    agent._control_chrome_tool(GOAL)

    names = {t["function"]["name"] for t in (_first_request(sent).get("tools") or [])}
    assert "browser_navigate" in names and "browser_snapshot" in names
    assert "edit_file" not in names and "run_command" not in names


def test_an_empty_goal_is_refused(browser):
    from glmcode.agent import ToolError
    agent, _ = browser
    with pytest.raises(ToolError, match="goal"):
        agent._control_chrome_tool("   ")


# ---------------------------------------------- the setter that was missing --

def test_setting_a_system_prompt_reaches_the_wire(browser):
    """The general fault behind the specific one. A prompt set on an agent has
    to reach messages[0], because that is what _messages_for_call sends --
    anything that only updates the cache is a change that looks applied and is
    not."""
    agent, _ = browser
    agent.set_system_prompt("You are something else entirely.")

    # By content, not by index: with a history this short the usage note is
    # next-to-last, so the system message is not position 0. What matters is
    # that the prompt on the wire is the new one.
    built = agent._messages_for_call()
    systems = [m["content"] for m in built if m["role"] == "system"]
    assert "You are something else entirely." in systems
    assert agent.system_prompt_text() == "You are something else entirely."


def test_setting_it_twice_replaces_rather_than_stacks(browser):
    agent, _ = browser
    agent.set_system_prompt("first")
    agent.set_system_prompt("second")

    systems = [m for m in agent.messages if m["role"] == "system"]
    assert len(systems) == 1 and systems[0]["content"] == "second"


def test_it_installs_one_even_when_there_is_no_history(browser):
    agent, _ = browser
    agent.messages = []
    agent.set_system_prompt("only thing here")
    assert agent.messages[0] == {"role": "system", "content": "only thing here"}
