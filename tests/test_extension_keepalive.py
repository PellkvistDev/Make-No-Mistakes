"""The extension has to still be there when the app calls, and say so.

Reported from a real machine: `browser_tabs` failed with "the browser did not
answer 'tabs' in time" while Settings said Connected.

An MV3 service worker is killed after 30 seconds idle, and the socket dies with
it -- so the extension speaks on a timer to hold it open. Three things about
that were wrong, and each of them ends in the same place: a socket the browser
keeps answering at the protocol level, with nothing behind it.

Read from the shipped source. There is no Chrome here, and the failures are
about which timer is used and when -- both of which are visible in the text.
"""

import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
BG = (EXT / "background.js").read_text(encoding="utf-8")


def _body(sig: str) -> str:
    start = BG.index(sig)
    return BG[start:BG.index("\n}\n", start)]


# ------------------------------------------- the alarm is a heartbeat -----

def test_a_successful_connection_does_not_clear_the_reconnect_alarm():
    """It used to, and that left a CONNECTED extension with no timer that
    outlives its worker. The keepalive interval holds the worker up until
    something reaps it anyway, and then nothing is left to wake it: the
    extension sits dead behind an open socket until the user happens to touch a
    tab. chrome.alarms is the only timer that survives a reap."""
    assert 'chrome.alarms.clear("reconnect")' not in BG


def test_the_alarm_is_armed_when_the_socket_opens():
    onopen = _body("ws.onopen = () => {")
    assert 'chrome.alarms.create("reconnect"' in onopen


def test_the_alarm_restarts_the_keepalive():
    """setInterval does not survive a reap any more than setTimeout does -- the
    file already says so about setTimeout, eighty lines up. A worker that came
    back without its interval would keep the connection open and be declared
    dead by the app fifty seconds later, over and over."""
    handler = BG[BG.index("chrome.alarms.onAlarm.addListener"):]
    handler = handler[:handler.index("\n});")]
    assert "startKeepalive()" in handler
    assert "keepaliveTimer" in handler


# ------------------------------------ re-dialling is not blocked forever --

def test_connect_guards_on_the_socket_being_usable_not_merely_present():
    """`if (socket) return` makes this an early return for the rest of the
    worker's life if a socket ever reaches CLOSING/CLOSED without onclose
    having run: the extension never re-dials, and the app goes on showing it as
    connected."""
    body = _body("function connect() {")
    assert "readyState" in body, "connect() still returns on any non-null socket"
    assert "WebSocket.OPEN" in body


# ------------------------------------------- what the keepalive is for ----

def test_it_speaks_from_javascript_rather_than_relying_on_pings():
    """A ping is answered by Chrome's network stack without the worker being
    woken, so it proves the BROWSER is alive and nothing about the extension.
    Only a message the worker sends itself does that."""
    assert re.search(r'send\(\s*\{\s*type:\s*"keepalive"', BG)


def test_the_keepalive_beats_faster_than_the_worker_is_reaped():
    """30 seconds idle is the deadline; the message has to land before it."""
    ms = int(re.search(r"const KEEPALIVE_MS = (\d+)", BG).group(1))
    assert 0 < ms < 30000


def test_the_app_gives_it_more_than_one_missed_beat():
    """A single dropped message under load is not a dead worker, and calling it
    one would flap the connection."""
    from glmcode.extension_bridge import WORKER_QUIET_AFTER
    ms = int(re.search(r"const KEEPALIVE_MS = (\d+)", BG).group(1))
    assert WORKER_QUIET_AFTER > (ms / 1000) * 2
