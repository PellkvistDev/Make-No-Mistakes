"""Cooperative wrap-up + the step-limit forced final report."""

from conftest import FakeResult, tool_call


def test_request_wrapup_forces_early_final_report(scripted_agent, events):
    agent_box = {}

    def script(n):
        # Would loop forever on tool calls if wrap-up didn't kick in.
        if n == 1:
            agent_box["a"].request_wrapup()
        return FakeResult([tool_call(f"c{n}")])

    agent = scripted_agent(script)
    agent_box["a"] = agent
    agent.run_turn({"role": "user", "content": "research everything forever"})

    assert events.wrapups == 1
    # The forced call (tools withheld) produced a real final message.
    assert agent.messages[-1]["role"] == "assistant"
    assert agent.messages[-1]["content"] == "(forced wrap-up report)"
    # It stopped after ~2 tool rounds, nowhere near the step limit.
    assert agent.client.n <= 3


def test_step_limit_still_forces_report(scripted_agent, events, monkeypatch):
    def script(n):
        return FakeResult([tool_call(f"c{n}")])  # never answers on its own

    agent = scripted_agent(script)
    monkeypatch.setattr(agent.cfg, "max_turns_per_request", 3)
    agent.run_turn({"role": "user", "content": "go"})

    assert agent.messages[-1]["role"] == "assistant"
    assert agent.messages[-1]["content"] == "(forced wrap-up report)"
    # warn() fired about hitting the step cap
    assert any("stopped after" in msg for lvl, msg in events.notices if lvl == "warn")


def test_wrapup_flag_cleared_between_turns(scripted_agent):
    agent = scripted_agent(lambda n: FakeResult(content="hi"))
    agent.request_wrapup()
    agent.run_turn({"role": "user", "content": "one"})
    # A stale flag must not force-wrap the NEXT turn.
    assert not agent.wrap_up_requested.is_set()
