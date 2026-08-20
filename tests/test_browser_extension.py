"""Driving the browser the user already has open, through the extension.

The DevTools port can only be opened at LAUNCH, so the older "use my own
browser" could never mean anything but "quit your browser and reopen it with
these flags". This is the path that needs none of that: the extension is
already inside the browser, so it dials the app and the app drives the tab the
user is looking at.

What matters is that NOTHING above the page object changed. The snapshot, the
stable refs, the region grouping, the error messages and the Browser Agent's
whole prompt are written against a Playwright page, so the extension is given
the same shape rather than the ops being taught a second way to work. These
tests drive the real _op_* methods over a real socket to prove that.
"""

import json
import time

import pytest

from glmcode.browser_session import BrowserError, BrowserSession
from glmcode.extension_bridge import ExtensionBridge

from test_extension_bridge import _Ext   # noqa: E402  (same dir)


PAGE = {
    "items": [
        {"ref": 1, "tag": "input", "label": "Search", "region": "main",
         "disabled": False, "type": "text"},
        {"ref": 2, "tag": "button", "label": "Go", "region": "main",
         "disabled": False, "type": "submit"},
        {"ref": 3, "tag": "select", "label": "Size", "region": "main",
         "disabled": False, "type": "", "options": ["S", "M", "L"]},
    ],
    "outline": ["Example"],
}


@pytest.fixture
def wired():
    """A BrowserSession whose browser is a fake extension on a real socket."""
    bridge = ExtensionBridge(ports=(0,), timeout=5)
    bridge.start()
    ext = _Ext(bridge.port)
    for _ in range(100):
        if bridge.connected:
            break
        time.sleep(0.02)
    assert bridge.connected

    calls = []

    def record(name):
        def fn(p):
            calls.append((name, p))
            return True
        return fn

    ext.handle("info", lambda p: {"url": "https://example.com", "title": "Example",
                                  "width": 1440, "height": 900})
    ext.handle("snapshot", lambda p: PAGE)
    ext.handle("exists", lambda p: any(i["ref"] == p["ref"] for i in PAGE["items"]))
    ext.handle("enabled", lambda p: True)
    for name in ("navigate", "click", "click_at", "fill", "select", "press", "back"):
        ext.handle(name, record(name))
    ext.handle("text", lambda p: "The page says hello.")
    ext.handle("screenshot", lambda p: "data:image/png;base64,aGk=")

    sess = BrowserSession(bridge=bridge)
    sess.calls = calls
    yield sess, ext
    sess.close()
    ext.close()
    bridge.stop()


# --------------------------------------------------------------------- #

def test_the_snapshot_is_the_same_one_the_model_already_knows(wired):
    """Same header, same numbered refs, same regions. The Browser Agent's
    prompt is written against this exact format -- a second, subtly different
    page description would be a different tool wearing the same name."""
    sess, _ = wired
    snap = sess.snapshot()
    assert "Page title: Example" in snap
    assert "URL: https://example.com" in snap
    assert "Main content:" in snap
    assert '[1] input "Search"' in snap
    assert '[2] button "Go"' in snap
    assert "(options: S, M, L)" in snap


def test_it_adopts_the_real_windows_size(wired):
    """browser_click_at REFUSES coordinates outside self.viewport, and the
    user's window is whatever size they left it."""
    sess, _ = wired
    sess.start()
    assert sess.viewport == (1440, 900)
    assert "Viewport: 1440x900 px" in sess.snapshot()


def test_navigate_goes_through_the_extension(wired):
    sess, _ = wired
    sess.navigate("example.com")
    # _op_navigate adds the scheme before it ever reaches the browser, exactly
    # as it does on the Playwright path.
    assert ("navigate", {"url": "https://example.com"}) in sess.calls


def test_click_addresses_the_element_by_its_ref(wired):
    sess, _ = wired
    sess.snapshot()
    sess.click(2)
    assert ("click", {"ref": 2}) in sess.calls


def test_typing_fills_and_can_submit(wired):
    sess, _ = wired
    sess.snapshot()
    sess.type_text(1, "laptops", submit=True)
    kinds = dict((n, p) for n, p in sess.calls)
    assert kinds["fill"]["text"] == "laptops"
    # submit is a separate keypress here, exactly as the Playwright path does
    # it (h.fill then h.press("Enter")), so the ops stay identical.
    assert kinds["press"]["key"] == "Enter"


def test_a_dropdown_is_chosen_not_typed_into(wired):
    sess, _ = wired
    sess.snapshot()
    sess.type_text(3, "M")
    assert ("select", {"ref": 3, "text": "M"}) in sess.calls


def test_a_checkbox_still_refuses_typing(wired):
    """The op-level guidance is unchanged; it never reaches the browser."""
    sess, ext = wired
    ext.handle("snapshot", lambda p: {"items": [
        {"ref": 9, "tag": "input", "label": "Agree", "region": "main",
         "disabled": False, "type": "checkbox"}], "outline": []})
    ext.handle("exists", lambda p: True)
    sess.snapshot()
    with pytest.raises(BrowserError) as e:
        sess.type_text(9, "yes")
    assert "browser_click(9)" in str(e.value)


def test_reading_the_page(wired):
    sess, _ = wired
    assert "The page says hello." in sess.read_text()


def test_a_screenshot_comes_back_as_real_bytes(wired, tmp_path):
    sess, _ = wired
    out = sess.screenshot(tmp_path / "shot.png")
    assert (tmp_path / "shot.png").read_bytes() == b"hi"
    assert out.endswith("shot.png")


def test_click_at_is_bounded_by_the_real_window(wired):
    """1440x900 is the user's window; the old 1280x800 default would have
    rejected a legitimate click as out of bounds."""
    sess, _ = wired
    sess.start()
    sess.click_at(1400, 880)
    assert ("click_at", {"x": 1400.0, "y": 880.0}) in sess.calls
    with pytest.raises(BrowserError) as e:
        sess.click_at(1500, 100)
    assert "outside the 1440x900 viewport" in str(e.value)


def test_a_stale_ref_fails_the_way_it_always_did(wired):
    sess, ext = wired
    sess.snapshot()
    ext.handle("exists", lambda p: False)
    with pytest.raises(BrowserError) as e:
        sess.click(2)
    assert "no longer on the page" in str(e.value)


def test_an_unknown_ref_never_reaches_the_browser(wired):
    sess, _ = wired
    sess.snapshot()
    with pytest.raises(BrowserError) as e:
        sess.click(99)
    assert "browser_snapshot" in str(e.value)
    assert not [c for c in sess.calls if c[0] == "click"]


def test_the_browser_going_away_is_reported_as_a_browser_error(wired):
    """Not a BridgeError leaking through: everything above this point handles
    BrowserError and nothing handles the transport's own exception type."""
    sess, ext = wired
    sess.start()
    ext.close()
    time.sleep(0.2)
    with pytest.raises(BrowserError):
        sess.snapshot()


def test_teardown_closes_nothing(wired):
    """It is the user's browser. There is nothing of ours to shut down, and
    the socket outlives any one session because other chats share it."""
    sess, ext = wired
    sess.start()
    sess.close()
    assert ext.sock.fileno() != -1


def test_arbitrary_javascript_is_refused_with_a_reason(wired):
    """Chrome forbids an extension from evaluating a string, so the snapshot is
    generated INTO the extension. A caller assuming a general evaluate() should
    be told that, not silently handed a snapshot."""
    sess, _ = wired
    sess.start()
    with pytest.raises(BrowserError) as e:
        sess._page.evaluate("() => document.title")
    assert "arbitrary JavaScript" in str(e.value)


def test_it_counts_as_the_users_browser(wired):
    """is_attached drives the Browser Agent's 'this is not a sandbox' note, and
    it must be true for this path too -- it is the MORE direct way of being in
    someone's real session."""
    sess, _ = wired
    assert sess.is_attached is True
