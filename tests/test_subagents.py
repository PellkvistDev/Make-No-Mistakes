"""Parallel sub-agents: report extraction, failure surfacing, aid uniqueness,
usage accounting, and the final-report stitcher."""

import pytest

from glmcode.agent import _final_report_text
from glmcode.api import ApiError
from glmcode.prompts import CONTINUE_NUDGE

from conftest import FakeResult, ScriptedClient


def test_happy_path_report_and_done(scripted_agent, events):
    coord = scripted_agent(allow_subagents=True)
    ScriptedClient.scripts = [lambda n: FakeResult(content="Final report: did the thing.")]
    out = coord._run_subagents([{"name": "worker", "task": "do it"}])
    assert "Final report: did the thing." in out
    assert events.subagent_events[-1][1] == "done"


def test_early_failure_is_error_not_done(scripted_agent, events):
    # A sub-agent whose calls all fail must surface as an ERROR with the real
    # reason -- not a bland "done" with "(no final report)". This was the
    # "shows running, does nothing, then done without a report" bug.
    def die(n):
        raise ApiError(429, "rate limited")

    coord = scripted_agent(allow_subagents=True)
    ScriptedClient.scripts = [die]
    out = coord._run_subagents([{"name": "researcher", "task": "look into X"}])

    assert events.subagent_events[-1][1] == "error"
    assert "FAILED" in out and ("429" in out or "rate limited" in out)
    # ...and the error also reached the sub-agent's own live thread.
    notices = [d for (_i, k, d) in events.streams if k == "notice"]
    assert any(d["level"] == "error" and "rate limited" in d["text"] for d in notices)


def test_aids_unique_across_spawn_calls(scripted_agent, events):
    # aids used to be plain "sa1"/"sa2" per index, reused verbatim by every
    # spawn_agents call in a chat -- so a second batch silently reused the
    # first batch's inspector threads/tabs in the UI.
    coord = scripted_agent(allow_subagents=True)
    ScriptedClient.scripts = [lambda n: FakeResult(content="one"),
                              lambda n: FakeResult(content="two")]
    coord._run_subagents([{"name": "a", "task": "t1"}])
    coord._run_subagents([{"name": "a", "task": "t2"}])
    aid1 = events.subagent_events[0][0]
    aid2 = events.subagent_events[2][0]
    assert aid1 != aid2
    assert aid1.startswith("sa") and "-" in aid1


def test_subagent_usage_folds_into_coordinator(scripted_agent):
    coord = scripted_agent(allow_subagents=True)
    ScriptedClient.scripts = [
        lambda n: FakeResult(content="r1", prompt_tokens=100, completion_tokens=40),
        lambda n: FakeResult(content="r2", prompt_tokens=200, completion_tokens=60),
    ]
    before_c = coord.session_usage.completion_tokens
    coord._run_subagents([{"name": "a", "task": "t1"}, {"name": "b", "task": "t2"}])
    # Both sub-agents' output tokens are now counted (they used to vanish).
    assert coord.session_usage.completion_tokens - before_c == 100
    assert coord.session_usage.prompt_tokens == 300


def test_no_task_is_an_error(scripted_agent, events):
    coord = scripted_agent(allow_subagents=True)
    coord._run_subagents([{"name": "empty"}])
    assert events.subagent_events[-1][1] == "error"


def test_final_report_text_stitches_continuation_splits():
    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "part one, "},
        {"role": "user", "content": CONTINUE_NUDGE},
        {"role": "assistant", "content": "part two."},
    ]
    assert _final_report_text(msgs) == "part one, part two."


def test_final_report_text_ignores_earlier_turns():
    msgs = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "an EARLIER answer"},
        {"role": "user", "content": "follow-up"},
        {"role": "assistant", "content": "the real final answer"},
    ]
    assert _final_report_text(msgs) == "the real final answer"
