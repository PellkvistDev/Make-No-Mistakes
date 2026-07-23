"""Thinking modes: low/medium do a single pass; high runs one self-review pass;
max runs up to 3 but stops early once a review changes nothing. Sub-agents never
refine. Model calls are scripted, no network."""

from glmcode.prompts import REFINE_NUDGE

from conftest import FakeResult


def _count_calls(agent):
    """A script that just answers, counting how many times the model was hit."""
    n = {"c": 0}

    def script(i):
        n["c"] += 1
        return FakeResult(content=f"answer {i}")
    agent.client._script = script
    return n


def _refine_msgs(agent):
    return [m for m in agent.messages
            if m.get("role") == "user" and REFINE_NUDGE in (m.get("content") or "")]


def test_medium_does_not_refine(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    agent.cfg.thinking_mode = "medium"
    n = _count_calls(agent)
    agent.run_turn({"role": "user", "content": "hi"})
    assert n["c"] == 1                 # answer only, no review
    assert _refine_msgs(agent) == []


def test_high_runs_one_review_pass(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    agent.cfg.thinking_mode = "high"
    n = _count_calls(agent)
    agent.run_turn({"role": "user", "content": "hi"})
    assert n["c"] == 2                 # answer + one review
    assert len(_refine_msgs(agent)) == 1


def test_max_stops_early_when_review_changes_nothing(scripted_agent):
    # Every pass just answers (no edits), so the first review finds nothing to
    # change -> Max stops instead of burning all 3 passes.
    agent = scripted_agent(allow_subagents=True)
    agent.cfg.thinking_mode = "max"
    n = _count_calls(agent)
    agent.run_turn({"role": "user", "content": "hi"})
    assert n["c"] == 2                 # answer + one (empty) review, then stop
    assert len(_refine_msgs(agent)) == 1


def test_config_migrates_old_thinking_bool(monkeypatch, tmp_path):
    import json
    from glmcode import config as cfgmod
    f = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", f)
    # An old config that predates thinking_mode: only the boolean.
    f.write_text(json.dumps({"thinking": False}))
    c = cfgmod.load_config()
    assert c.thinking_mode == "low" and c.thinking is False
    f.write_text(json.dumps({"thinking": True}))
    c = cfgmod.load_config()
    assert c.thinking_mode == "medium" and c.thinking is True
    # A new config's explicit mode is respected (and keeps `thinking` in sync).
    f.write_text(json.dumps({"thinking_mode": "high"}))
    c = cfgmod.load_config()
    assert c.thinking_mode == "high" and c.thinking is True


def test_subagents_never_refine(scripted_agent):
    agent = scripted_agent(allow_subagents=False)  # a sub-agent / worker
    agent.cfg.thinking_mode = "max"
    n = _count_calls(agent)
    agent.run_turn({"role": "user", "content": "hi"})
    assert n["c"] == 1                 # one pass, no reviews
    assert _refine_msgs(agent) == []
