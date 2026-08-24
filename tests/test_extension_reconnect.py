"""The extension has to survive its own service worker being killed.

Reported: "I installed the browser extension and told the agent to use my open
browser, but it opened a new Chrome window, blank."

The extension had connected to nothing and then died. An MV3 service worker is
terminated after 30 seconds idle, and a pending setTimeout does NOT survive
that -- so the ordinary install order (load the extension while the app isn't
listening yet) burned through a few retries, got terminated, and never tried
again. Nothing in the browser would have woken it.

There is no Chrome here to run this in, so these read the shipped source. That
is worth doing anyway: the failure is the ABSENCE of a wake-up mechanism, and
absence is exactly what a runtime test of the happy path never notices.
"""

import json
import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
BG = (EXT / "background.js").read_text(encoding="utf-8")
MANIFEST = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))


def test_reconnect_uses_an_alarm_and_not_only_a_timeout():
    """setTimeout dies with the worker. chrome.alarms is the one timer that
    outlives it, and it is the whole reason this reconnects at all."""
    assert "chrome.alarms.create" in BG
    assert "chrome.alarms.onAlarm.addListener" in BG


def test_the_alarm_permission_is_declared():
    """Without it chrome.alarms is undefined and scheduleRetry throws -- which
    would break the retry path it was added to fix."""
    assert "alarms" in MANIFEST["permissions"]


def test_the_alarm_stays_armed_once_connected():
    """This used to assert the opposite, and the reason it gave was true when
    it was written: an alarm firing every thirty seconds for a connection that
    is already up is pure cost.

    The keepalive is what changed it. The worker now speaks every twenty
    seconds to hold itself open, so while connected it is deliberately kept
    ALIVE -- there is nothing asleep for the alarm to wake, and its cost is
    zero. What clearing it did cost was the only timer that outlives a reap: a
    connected extension had nothing left to bring it back, so when something
    finally killed the worker anyway it sat dead behind an open socket, which
    the app went on reporting as Connected, until the user happened to touch a
    tab. It is a heartbeat, not just a retry."""
    assert 'chrome.alarms.clear("reconnect")' not in BG
    onopen = BG[BG.index("ws.onopen = () => {"):]
    assert 'chrome.alarms.create("reconnect"' in onopen[:onopen.index("\n  };")]


def test_browsing_wakes_a_reconnect_too():
    """The alarm's floor is 30 seconds. Anything the user does in the browser
    wakes the worker anyway, so a reconnect can happen the moment they touch a
    tab rather than up to half a minute later."""
    assert "chrome.tabs.onActivated" in BG
    assert "chrome.windows.onFocusChanged" in BG


def test_connect_is_safe_to_call_repeatedly():
    """Every one of those wake-ups calls connect(). If it opened a second
    socket each time, browsing would spray connections at the app.

    Stated as the property rather than as the line that used to express it.
    `if (paused || socket) return` also returned early for a socket in
    CLOSING or CLOSED -- so one that got there without onclose having run made
    this a permanent early return, and the extension never re-dialled. The
    no-op has to hold for a USABLE socket and only for one."""
    body = BG[BG.index("function connect()"):]
    first = body[:body.index("\n}")]
    assert "paused" in first and "return" in first
    assert "WebSocket.OPEN" in first and "WebSocket.CONNECTING" in first, \
        "connect() must no-op on a live socket -- and only on a live one"
    assert not re.search(r"if \(paused \|\| socket\) return", first), \
        "a CLOSED socket must not block re-dialling for the worker's lifetime"


def test_the_ports_match_the_python_side():
    """The two lists are written twice, and a mismatch is invisible from either
    file -- it surfaces as an extension that never connects to an app that is
    definitely running."""
    from glmcode.extension_bridge import PORTS
    listed = re.search(r"const PORTS = \[([^\]]+)\]", BG).group(1)
    assert [int(p) for p in listed.split(",")] == list(PORTS)


def test_the_paused_state_survives_a_worker_restart():
    """Pause is a promise to the user that the agent is not driving their
    browser. A flag in a variable would quietly un-pause every time Chrome
    recycled the worker."""
    assert "chrome.storage.local.set({ paused })" in BG
    assert 'chrome.storage.local.get("paused")' in BG
