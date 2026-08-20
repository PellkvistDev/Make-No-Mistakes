"""my_tabs: answering "what do I have open?" without a browser sub-agent.

Reported, from a real machine: "when I ask it what tabs I have open, it says
that it doesn't have access, then I tell it that it should use control_chrome
and then it fails."

Both halves of that were true. The coding agent had exactly one browser tool --
control_chrome -- which spawns a whole Browser Agent; nothing told it that the
user's own tabs were reachable at all, so asked a question it answered with a
refusal. And a question does not deserve a sub-agent anyway.
"""

import time

import pytest

from glmcode import browser_extension as bx
from glmcode import tools
from glmcode.extension_bridge import ExtensionBridge

from test_extension_bridge import _Ext   # noqa: E402  (same directory)


TABS = [
    {"id": 3, "title": "Pull requests", "url": "https://github.com/a/b/pulls",
     "active": True, "usable": True},
    {"id": 4, "title": "Docs", "url": "https://example.com/docs", "active": False,
     "usable": True},
]


@pytest.fixture(autouse=True)
def _clean():
    bx.reset_for_tests()
    yield
    bx.reset_for_tests()


@pytest.fixture
def connected(monkeypatch):
    bridge = ExtensionBridge(ports=(0,), timeout=5)
    bridge.start()
    ext = _Ext(bridge.port)
    for _ in range(100):
        if bridge.connected:
            break
        time.sleep(0.02)
    assert bridge.connected
    monkeypatch.setattr(bx, "_bridge", bridge)
    yield bridge, ext
    ext.close()
    bridge.stop()


def test_it_lists_the_tabs(connected):
    _, ext = connected
    ext.handle("tabs", lambda p: TABS)
    out = tools.my_tabs()
    assert "[3] Pull requests" in out and "[4] Docs" in out
    assert "github.com/a/b/pulls" in out
    assert "<- frontmost" in out


def test_it_points_at_control_chrome_for_doing_things(connected):
    """The question and the action are different tools, and the answer to one
    should hand you the other."""
    _, ext = connected
    ext.handle("tabs", lambda p: TABS)
    assert "control_chrome" in tools.my_tabs()


def test_with_no_extension_it_explains_instead_of_refusing():
    """"I don't have access" is what the model said before this existed, and it
    is useless: it names no cause and no fix."""
    out = tools.my_tabs()
    assert "Not connected to your browser" in out
    assert "Settings -> Browser" in out
    assert "control_chrome" in out          # what it CAN still do


def test_a_browser_that_stops_answering_is_reported_as_such(connected, monkeypatch):
    """The reported symptom exactly: Settings said connected while everything
    failed. It must not hang, and it must not claim there are no tabs."""
    import glmcode.extension_bridge as eb
    monkeypatch.setattr(eb, "STALE_AFTER", 0.6)
    monkeypatch.setattr(eb, "PING_EVERY", 0.15)
    bridge, ext = connected
    ext.deaf = True
    for _ in range(60):
        if not bridge.connected:
            break
        time.sleep(0.05)
    out = tools.my_tabs()
    assert "Not connected" in out


def test_an_empty_browser_says_so(connected):
    _, ext = connected
    ext.handle("tabs", lambda p: [])
    assert "no open tabs" in tools.my_tabs()


def test_it_is_offered_to_the_coding_agent_and_costs_no_permission():
    """It only reads a tab list. Gating it behind a prompt for that would train
    the user to approve things."""
    names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
    assert "my_tabs" in names
    assert tools.TOOL_FUNCTIONS["my_tabs"] is tools.my_tabs
    assert "my_tabs" in tools.READONLY_TOOLS


def test_the_prompt_tells_it_not_to_claim_it_has_no_access():
    from glmcode.prompts import SYSTEM_PROMPT as sp
    assert "my_tabs" in sp
    assert "no access" in sp
    # and that acting on one of those pages is control_chrome's job
    assert "control_chrome — it drives that" in sp
