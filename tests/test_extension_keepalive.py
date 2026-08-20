"""The service worker must be kept alive by the EXTENSION, not by the app.

Reported, with the connection log that proves it:

  19:02:07 connected → 19:02:37 dropped   (30s)
  19:02:55 connected → 19:03:25 dropped   (30s)

Thirty seconds is the MV3 service worker idle timeout, exactly. The app's
heartbeat kept the SOCKET alive, but a WebSocket ping is answered by Chrome's
network stack without ever waking the worker -- so the worker was reaped out
from under a socket that looked perfectly healthy, and the socket died with it.

The documented fix is that the extension itself must send a message before each
deadline: activity the worker performs is what resets its own timer.

Read from the shipped source, because the failure is the ABSENCE of a
mechanism, and there is no Chrome here to observe it in.
"""

import json
import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
BG = (EXT / "background.js").read_text(encoding="utf-8")


def test_the_extension_speaks_on_its_own_schedule():
    assert "KEEPALIVE_MS" in BG
    assert "startKeepalive" in BG and "stopKeepalive" in BG


def test_it_speaks_well_inside_the_thirty_second_deadline():
    """A margin, not a race: the worker dies at 30s of silence, and a timer
    that fires at 29 would lose to any scheduling jitter."""
    ms = int(re.search(r"const KEEPALIVE_MS = (\d+)", BG).group(1))
    assert 5000 <= ms <= 25000, f"{ms}ms is not a safe margin under 30s"


def test_the_keepalive_starts_when_the_socket_opens():
    onopen = BG[BG.index("ws.onopen"):BG.index("ws.onmessage")]
    assert "startKeepalive()" in onopen


def test_it_stops_when_the_socket_closes():
    """A timer left running against a dead socket keeps the worker alive for
    nothing, which is the opposite of the problem but still wrong."""
    onclose = BG[BG.index("ws.onclose"):BG.index("ws.onerror")]
    assert "stopKeepalive()" in onclose


def test_the_app_ignores_the_keepalive_rather_than_choking_on_it():
    """It carries no id, so it is not a reply to anything. It just has to
    arrive -- and to count as the extension being alive."""
    from glmcode.extension_bridge import ExtensionBridge
    b = ExtensionBridge(ports=(0,))
    b._dispatch({"type": "keepalive"})        # must not raise
    assert b.hello == {}                      # and must not be mistaken for hello


# --------------------------------------------------------------------- #
# "image readback failed"

def test_the_tab_is_brought_forward_before_it_is_photographed():
    """captureVisibleTab photographs what is ON SCREEN. With the window behind
    the app -- the normal state here, since the person is looking at the app --
    Chrome has nothing rendered to read back and fails with "image readback
    failed"."""
    cap = BG[BG.index("async function capture("):BG.index("// -- commands")]
    assert "chrome.tabs.update" in cap and "active: true" in cap
    assert "focused: true" in cap
    assert 'state: "normal"' in cap, "a minimised window has nothing to capture"


def test_it_retries_once_before_giving_up():
    cap = BG[BG.index("async function capture("):BG.index("// -- commands")]
    assert "attempt < 2" in cap


def test_a_failed_capture_points_at_what_still_works():
    """A screenshot is never the only way to see a page: the text and the
    snapshot are both still there, and the agent should be told so rather than
    just failing the step."""
    cap = BG[BG.index("async function capture("):BG.index("// -- commands")]
    assert "browser_read" in cap and "browser_snapshot" in cap


def test_the_screenshot_is_not_sent_as_a_lossless_png():
    cap = BG[BG.index("async function capture("):BG.index("// -- commands")]
    assert '"jpeg"' in cap


# --------------------------------------------------------------------- #
# The button that opened an empty window

def test_nothing_pretends_it_can_raise_another_apps_window():
    """Reported: "press 'bring chrome to the front' and that opens an empty
    chrome window". Two attempts, both worse than doing nothing: passing
    chrome://extensions on the command line gets the URL DROPPED and an empty
    window opened, and launching with no argument at all opens a fresh blank
    window on Windows."""
    from glmcode import installed_browsers
    ok, why = installed_browsers.open_browser("/anything")
    assert ok is False
    assert "can't bring another app's window forward" in why
    src = pathlib.Path(installed_browsers.__file__).read_text(encoding="utf-8")
    assert "subprocess.Popen" not in src, \
        "nothing here should be launching a browser any more"
