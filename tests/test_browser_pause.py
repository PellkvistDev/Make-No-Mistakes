"""Pause / take-over / resume for the Browser Agent: the human can freeze the
loop at a safe checkpoint, drive the (idle, open) browser themselves, then
resume the SAME agent -- which is told the page may have changed."""

import threading

from conftest import FakeResult


def test_request_pause_requires_pausable(scripted_agent):
    agent = scripted_agent()
    assert agent.request_pause() is False   # normal agents can't be paused
    assert agent.request_resume() is False
    agent.pausable = True
    assert agent.request_pause() is True
    assert agent.is_paused is True


def test_maybe_pause_noop_when_not_pausable(scripted_agent):
    agent = scripted_agent()
    agent._pause_flag.set()          # even if the flag were somehow set...
    agent._maybe_pause()             # ...a non-pausable agent doesn't block
    assert not any("Resumed" in (m.get("content") or "") for m in agent.messages)


def test_maybe_pause_blocks_until_resume_then_injects_note(scripted_agent):
    agent = scripted_agent()
    agent.pausable = True
    agent.request_pause()
    done = threading.Event()
    threading.Thread(target=lambda: (agent._maybe_pause(), done.set()),
                     daemon=True).start()
    # Still blocked a moment later (the human is "driving the browser").
    assert not done.wait(0.4)
    assert agent.is_paused is True
    agent.request_resume()
    assert done.wait(2.0)                       # unblocks promptly on resume
    assert agent.is_paused is False
    # The resumed agent is told to re-perceive the page.
    notes = [m for m in agent.messages
             if "Resumed by the user" in (m.get("content") or "")]
    assert len(notes) == 1
    assert "browser_snapshot" in notes[0]["content"]


def test_cancel_escapes_pause_without_note(scripted_agent):
    agent = scripted_agent()
    agent.pausable = True
    agent.request_pause()
    done = threading.Event()
    threading.Thread(target=lambda: (agent._maybe_pause(), done.set()),
                     daemon=True).start()
    assert not done.wait(0.4)
    agent.cancel.set()                          # user cancelled instead of resuming
    assert done.wait(2.0)
    assert not any("Resumed" in (m.get("content") or "") for m in agent.messages)


def test_coordinator_pause_resume_targets_the_browser_agent(scripted_agent, events):
    coord = scripted_agent(allow_subagents=True)
    # No browser agent running yet.
    assert coord.pause_browser_agent() is False
    assert coord.resume_browser_agent() is False

    # Register a fake running Browser Agent, as _run_browser_subagent would.
    sub = scripted_agent()
    sub.pausable = True
    coord._active_subagents["chrome-1"] = sub
    coord._browser_agent_aid = "chrome-1"

    assert coord.pause_browser_agent() is True
    assert sub.is_paused is True
    # ...and the UI was told the browser agent is paused.
    assert any(s == "paused" for (_id, s, _sum) in events.subagent_events)

    assert coord.resume_browser_agent() is True
    assert sub._resume_flag.is_set() is True
    assert any(s == "running" for (_id, s, _sum) in events.subagent_events)


def test_browser_subagent_is_marked_pausable(scripted_agent, monkeypatch):
    """The Browser Agent spawned by control_chrome must be pausable and
    registered so the coordinator can reach it."""
    coord = scripted_agent(allow_subagents=True)

    captured = {}

    # Intercept run_turn to inspect the sub-agent at the moment it "runs",
    # while it's still in the active registry, then return immediately.
    import glmcode.agent as agent_mod
    real_run = agent_mod.Agent.run_turn

    def fake_run(self, user_message):
        if getattr(self, "pausable", False):
            captured["pausable"] = True
            captured["registered_aid"] = coord._browser_agent_aid
            captured["in_registry"] = coord._browser_agent_aid in coord._active_subagents
            # produce a report so _run_browser_subagent returns cleanly
            self.messages.append({"role": "assistant", "content": "did it"})
            return
        return real_run(self, user_message)

    monkeypatch.setattr(agent_mod.Agent, "run_turn", fake_run)

    class FakeSession:
        is_open = True
        def start(self): pass

    coord.browser_session = FakeSession()
    out = coord._control_chrome_tool("do a browser thing")
    assert "did it" in out
    assert captured.get("pausable") is True
    assert captured.get("in_registry") is True
    # cleaned up afterward
    assert coord._browser_agent_aid is None
