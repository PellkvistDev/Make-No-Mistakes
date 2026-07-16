"""Mid-turn steering: queueing, framing, one-at-a-time, and return-on-miss."""

from glmcode.prompts import STEER_NUDGE_TEMPLATE

from conftest import FakeResult, tool_call


def test_steer_injected_after_tool_round_with_framing(scripted_agent, events):
    agent_box = {}

    def script(n):
        if n == 1:
            # Queue the steer while the turn is mid-flight -- deterministic,
            # no sleeps: it lands before this round's tool results return.
            agent_box["a"].steer("also check the tests folder")
            return FakeResult([tool_call("c1")])
        return FakeResult(content="done")

    agent = scripted_agent(script)
    agent_box["a"] = agent
    agent.run_turn({"role": "user", "content": "refactor auth"})

    steer_msgs = [m for m in agent.messages
                  if m.get("role") == "user" and "also check the tests folder" in str(m.get("content"))]
    assert len(steer_msgs) == 1
    content = steer_msgs[0]["content"]
    # Wrapped as an in-task tip, not a bare instruction (the bare version
    # made the model treat it as a brand-new task and blow past scope).
    assert content == STEER_NUDGE_TEMPLATE.format(text="also check the tests folder")
    assert "NOT a new task" in content
    # The UI event carries ONLY the raw text, no framing boilerplate.
    assert events.steered_texts == ["also check the tests folder"]


def test_only_one_steer_queued_at_a_time(scripted_agent):
    agent = scripted_agent()
    assert agent.steer("first") is True
    assert agent.steer("second") is False  # rejected while one is pending
    agent.clear_steer()
    assert agent.steer("third") is True


def test_undelivered_steer_returned_when_turn_ends(scripted_agent, events):
    # Model answers in plain text immediately -- no tool round ever happens,
    # so a queued steer has nothing to attach to and must be handed back
    # (NOT silently cached into the next, unrelated turn).
    agent_box = {}

    def script(n):
        agent_box["a"].steer("never delivered")
        return FakeResult(content="all done")

    agent = scripted_agent(script)
    agent_box["a"] = agent
    agent.run_turn({"role": "user", "content": "quick question"})

    assert events.steer_returned_texts == ["never delivered"]
    assert events.steered_texts == []
    # And it must NOT leak into the next turn's messages.
    agent.run_turn({"role": "user", "content": "second turn"})
    leaked = [m for m in agent.messages if "never delivered" in str(m.get("content"))]
    assert leaked == []


def test_empty_steer_rejected(scripted_agent):
    agent = scripted_agent()
    assert agent.steer("   ") is False
    assert agent.steer("") is False
