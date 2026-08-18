"""Every request ends on the user's turn, not on a note about token budgets.

The context-usage figure is sent at the END of the message list rather than
inside the system prompt, and for a good reason: the figure changes every turn,
so putting it first gave every request a different prefix from the last and no
provider's prefix cache could ever hit. That part was right.

Putting it dead last was not. A request has to end on the user's turn (or a
tool result), and Google's OpenAI-compatibility layer does not treat a trailing
`system` message the way z.ai does. Sub-agents showed it at its worst: their
very first request is

    [system prompt, user(the mission), system(context usage)]

so the last thing the model read was a sentence about token budgets, and it
answered that one -- "I'm Gemini, what would you like me to do?" -- with the
mission sitting directly above it, ignored. A coordinator that handed out three
missions got back three sub-agents asking what the task was. On z.ai the same
history worked, which is what made it look like a model problem.

The note now goes next-to-last. In any conversation long enough for a prefix
cache to matter that leaves the same stable prefix, so nothing is given up.
"""

import sys

import pytest

sys.path.insert(0, "tests")

from conftest import FakeResult, ScriptedClient  # noqa: E402

from glmcode.agent import Agent  # noqa: E402
from glmcode.api import RateLimiter  # noqa: E402
from glmcode.config import Config  # noqa: E402
from glmcode.events import AgentEvents  # noqa: E402


@pytest.fixture
def spy(monkeypatch):
    """An agent whose outgoing requests are captured."""
    import glmcode.agent as agent_mod
    sent = []

    class Spy(ScriptedClient):
        def chat(self, **kw):
            sent.append(kw)
            return FakeResult(content="done")

    monkeypatch.setattr(agent_mod, "ZaiClient", Spy)
    ScriptedClient.scripts = []
    agent = Agent(Config(), Spy(), events=AgentEvents(), allow_subagents=True)
    agent.set_mode("yolo")
    yield agent, sent
    ScriptedClient.scripts = []


def _roles(sent):
    return [m["role"] for m in sent[-1]["messages"]]


def test_a_plain_turn_ends_on_the_user_message(spy):
    agent, sent = spy
    agent.run_turn({"role": "user", "content": "what does this repo do?"})
    assert _roles(sent)[-1] == "user"


def test_a_subagent_s_first_request_ends_on_its_mission(spy):
    """The case that broke. The mission is the only thing the sub-agent is ever
    told, and it was not the last thing in the request."""
    agent, sent = spy
    mission = "Add a dark mode toggle.\nPersist it to localStorage.\nCover it with a test."
    agent._run_single_subagent("worker-one", mission, RateLimiter(), "sa1")

    messages = sent[-1]["messages"]
    assert messages[-1]["role"] == "user"
    assert mission in messages[-1]["content"], \
        "the last thing the model reads must be the mission"


def test_the_usage_note_is_still_sent(spy):
    """Moving it must not quietly drop it -- the model needs to know when it is
    close to the limit, which is what makes compact_context reachable."""
    agent, sent = spy
    agent.run_turn({"role": "user", "content": "hello"})
    joined = " ".join(m["content"] for m in sent[-1]["messages"]
                      if isinstance(m.get("content"), str))
    assert "Context usage" in joined


def test_the_note_sits_next_to_last(spy):
    agent, sent = spy
    agent.run_turn({"role": "user", "content": "hello"})
    messages = sent[-1]["messages"]
    assert "Context usage" in messages[-2]["content"]


def test_the_prefix_before_the_newest_turn_is_unchanged_between_requests(spy):
    """Why the note was moved to the end in the first place. A figure that
    changes every turn must not sit near the front, or the provider's prefix
    cache -- which only matches an identical run of LEADING tokens -- can never
    hit, and this app re-sends ~12,400 tokens of system prompt and tool schemas
    on every single request."""
    agent, sent = spy
    agent.run_turn({"role": "user", "content": "first question"})
    agent.run_turn({"role": "user", "content": "second question"})

    first, second = sent[0]["messages"], sent[-1]["messages"]
    # Everything up to the first request's own newest turn is a stable prefix.
    shared = len(first) - 2
    assert shared > 0
    assert first[:shared] == second[:shared]


def test_an_empty_history_still_produces_a_usable_request(spy):
    """Nothing to sit next to. It must not index off the end of an empty list."""
    agent, _ = spy
    agent.messages = []
    assert agent._messages_for_call() == [
        {"role": "system", "content": agent._usage_note()}]


def test_a_tool_result_is_allowed_to_be_last(spy):
    """Mid-loop the newest message is a tool reply, and that is a legitimate
    place for a request to end -- the note must not be wedged after it either."""
    agent, sent = spy
    agent.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "a.py"},
    ]
    built = agent._messages_for_call()
    assert built[-1]["role"] == "tool"
    assert "Context usage" in built[-2]["content"]
