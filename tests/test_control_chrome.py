"""control_chrome delegates a goal to a specialized Browser Agent that drives
the chat's persistent BrowserSession. These tests use a FAKE session (no real
Chromium) and scripted models to verify the whole delegation: permission
gating, restricted tools, action routing, and report bubbling."""

import pytest

from glmcode.permissions import PermissionEngine
from glmcode.tools import BROWSER_AGENT_SCHEMAS

from conftest import FakeResult, ScriptedClient, tool_call


# -- permission gating --------------------------------------------------- #

def _never(*a, **k):
    raise AssertionError("should not prompt")


def test_control_chrome_prompts_in_ask_mode():
    eng = PermissionEngine(mode="ask")
    asked = {}
    def asker(title, preview, always_label=None):
        asked["preview"] = preview
        return ("n", "")
    d = eng.check("control_chrome", {"goal": "log in", "start_url": "https://x.com"}, asker)
    assert d.allowed is False
    assert "log in" in asked["preview"] and "https://x.com" in asked["preview"]


@pytest.mark.parametrize("mode", ["autoedit", "yolo"])
def test_control_chrome_auto_in_autoedit_and_yolo(mode):
    eng = PermissionEngine(mode=mode)
    assert eng.check("control_chrome", {"goal": "g"}, _never).allowed is True


@pytest.mark.parametrize("mode", ["ask", "autoedit", "yolo"])
def test_browser_action_tools_never_prompt(mode):
    # They only run inside an already-approved browser sub-agent.
    eng = PermissionEngine(mode=mode)
    for name in ("browser_navigate", "browser_click", "browser_type",
                 "browser_snapshot", "browser_read", "browser_key",
                 "browser_screenshot"):
        assert eng.check(name, {}, _never).allowed is True


# -- the fake browser ---------------------------------------------------- #

class FakeBrowserSession:
    def __init__(self):
        self.is_open = True
        self.navigated = []
        self.clicks = []
        self.closed = False

    def start(self):
        pass

    def navigate(self, url):
        self.navigated.append(url)
        return 'Page title: Shop\nURL: %s\n[1] button "Buy"' % url

    def click(self, ref):
        self.clicks.append(ref)
        return "clicked, new snapshot"

    def close(self):
        self.closed = True


# -- full delegation ----------------------------------------------------- #

def test_control_chrome_spawns_restricted_browser_agent(scripted_agent):
    seen_tools = {}

    def browser_script(i):
        # first call: the model decides to navigate; capture the tool menu it
        # was offered so we can assert it's ONLY the browser tools.
        if i == 1:
            return FakeResult(tool_calls=[tool_call(
                "b1", "browser_navigate", '{"url": "https://shop.test"}')])
        if i == 2:
            return FakeResult(tool_calls=[tool_call("b2", "browser_click", '{"ref": 1}')])
        return FakeResult(content="Bought it. Final URL https://shop.test/thanks.")

    # Wrap the browser script so we can capture the tools it was handed.
    def capturing_browser_script(i):
        return browser_script(i)

    coord_calls = iter([
        FakeResult(tool_calls=[tool_call(
            "c1", "control_chrome",
            '{"goal": "buy the widget", "start_url": "https://shop.test"}')]),
        FakeResult(content="Done — the browser agent bought the widget."),
    ])

    agent = scripted_agent(lambda i: next(coord_calls), allow_subagents=True)
    agent.set_mode("yolo")
    # Queue AFTER building the coordinator (whose client also pops the queue):
    # the browser sub-agent constructs its own ZaiClient, which pops this.
    ScriptedClient.scripts = [capturing_browser_script]

    fake = FakeBrowserSession()
    agent.browser_session = fake

    # Capture the tool menu handed to the browser sub-agent by wrapping chat().
    orig_chat = ScriptedClient.chat
    def spy_chat(self, **kw):
        if kw.get("tools") is not None and "browser" not in seen_tools:
            names = [t["function"]["name"] for t in kw["tools"]]
            if any(n.startswith("browser_") for n in names):
                seen_tools["browser"] = names
        return orig_chat(self, **kw)
    ScriptedClient.chat = spy_chat
    try:
        agent.run_turn({"role": "user", "content": "buy me the widget"})
    finally:
        ScriptedClient.chat = orig_chat

    # The browser agent actually drove the shared session...
    assert fake.navigated == ["https://shop.test"]
    assert fake.clicks == [1]
    # ...it was restricted to exactly the browser tools (no read_file/edit/etc.)
    assert seen_tools["browser"] == [s["function"]["name"] for s in BROWSER_AGENT_SCHEMAS]
    assert all(n.startswith("browser_") for n in seen_tools["browser"])
    # ...its report bubbled back into the coordinator as the tool result...
    joined = "\n".join(
        m.get("content") or "" for m in agent.messages if m.get("role") == "tool")
    assert "Bought it" in joined and "thanks" in joined
    # ...and the coordinator gave its own final answer.
    finals = [m.get("content") for m in agent.messages
              if m.get("role") == "assistant" and m.get("content")]
    assert any("bought the widget" in c for c in finals)


def test_control_chrome_needs_a_goal(scripted_agent):
    from glmcode.tools import ToolError
    agent = scripted_agent(allow_subagents=True)
    with pytest.raises(ToolError):
        agent._control_chrome_tool("   ")


def test_control_chrome_reports_launch_failure(scripted_agent, monkeypatch):
    from glmcode.tools import ToolError
    agent = scripted_agent(allow_subagents=True)
    def boom():
        raise RuntimeError("no display")
    monkeypatch.setattr(agent, "_ensure_browser_session", boom)
    with pytest.raises(ToolError, match="Could not start the browser"):
        agent._control_chrome_tool("do a thing")


def test_browser_action_requires_open_session(scripted_agent):
    from glmcode.tools import ToolError
    agent = scripted_agent(allow_subagents=True)
    agent.browser_session = None
    with pytest.raises(ToolError, match="not open"):
        agent._browser_action("browser_snapshot", {})


def test_close_browser_tears_down(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    fake = FakeBrowserSession()
    agent.browser_session = fake
    agent.close_browser()
    assert fake.closed is True
    assert agent.browser_session is None


def test_state_changing_actions_emit_a_browser_frame(scripted_agent):
    agent = scripted_agent(allow_subagents=True)
    frames = []
    agent.events.browser_frame = lambda url="", image="": frames.append((url, image))

    class Sess:
        is_open = True
        def navigate(self, url): return "snap"
        def click(self, ref): return "snap"
        def snapshot(self): return "snap-only"
        def screenshot_b64(self, max_width=520): return "data:image/jpeg;base64,AAAA"
        def current_url(self): return "https://x.test"

    agent.browser_session = Sess()
    agent._browser_action("browser_navigate", {"url": "https://x.test"})
    assert frames[-1] == ("https://x.test", "data:image/jpeg;base64,AAAA")
    agent._browser_action("browser_click", {"ref": 1})
    assert len(frames) == 2
    # A read-only action (snapshot) pushes no frame -- the page didn't change.
    frames.clear()
    agent._browser_action("browser_snapshot", {})
    assert frames == []


def test_subagents_do_not_get_control_chrome(scripted_agent):
    # A normal (non-coordinator) sub-agent's schema must exclude control_chrome.
    sub = scripted_agent(allow_subagents=False)
    names = [s["function"]["name"] for s in sub.tool_schemas]
    assert "control_chrome" not in names
    assert "spawn_agents" not in names
