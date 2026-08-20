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
import pathlib
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


# --------------------------------------------------------------------- #
# Reported: "I installed the extension and told it to use my open browser,
# and it opened a new blank Chrome window."
#
# Three separate faults, each of which fails in silence.

import types  # noqa: E402

from glmcode import browser_extension as bx  # noqa: E402
from glmcode.config import Config  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_bridge_module():
    bx.reset_for_tests()
    yield
    bx.reset_for_tests()


def test_the_port_is_opened_at_boot_not_when_settings_is_first_looked_at():
    """The bridge used to start lazily, from the Settings panel's status call.
    So the ordinary launch -- app starts, setting already on, nobody opens
    Settings -- left NOTHING for the browser to connect to, and the extension
    sat there unable to reach a thing."""
    import sys
    import types as t
    sys.modules.setdefault("webview", t.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app

    cfg = Config()
    cfg.browser_use_mine = True
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = cfg
    assert bx.bridge(start=False) is None          # nothing listening yet
    # The one line boot() runs; calling boot() itself would drag in the whole app.
    bx.bridge(start=bx.enabled(api._cfg))
    assert bx.bridge(start=False) is not None
    assert bx.bridge(start=False).port


def test_boot_opens_nothing_when_the_setting_is_off():
    """Someone who never turns this on never has a port open."""
    cfg = Config()
    bx.bridge(start=bx.enabled(cfg))
    assert bx.bridge(start=False) is None


def test_falling_back_to_a_launched_browser_says_so_out_loud():
    """The observable symptom was a second window opening with a blank tab, and
    nothing about that says 'your extension isn't connected'."""
    import glmcode.agent as agent_mod

    built, warnings = {}, []

    class FakeSession:
        def __init__(self, **kw):
            built.update(kw)
            self.is_open = True

        def start(self):
            pass

    fake_mod = types.ModuleType("glmcode.browser_session")
    fake_mod.BrowserSession = FakeSession
    import sys
    sys.modules["glmcode.browser_session"] = fake_mod
    try:
        ag = agent_mod.Agent.__new__(agent_mod.Agent)
        ag.browser_session = None
        ag.cfg = Config()
        ag.cfg.browser_use_mine = True          # on, but nothing connected
        ag.events = types.SimpleNamespace(info=lambda m: None,
                                          warn=lambda m: warnings.append(m))
        ag._ensure_browser_session()
    finally:
        sys.modules.pop("glmcode.browser_session", None)

    assert warnings, "it fell back to a separate browser without saying anything"
    said = warnings[0]
    assert "isn't connected" in said
    assert "separate browser window" in said
    # And it still worked -- the fallback is right, the silence was the bug.
    assert built.get("connect_url") is None and "headless" in built


def test_no_warning_when_the_user_never_asked_for_their_own_browser():
    import glmcode.agent as agent_mod

    warnings = []

    class FakeSession:
        def __init__(self, **kw):
            self.is_open = True

        def start(self):
            pass

    fake_mod = types.ModuleType("glmcode.browser_session")
    fake_mod.BrowserSession = FakeSession
    import sys
    sys.modules["glmcode.browser_session"] = fake_mod
    try:
        ag = agent_mod.Agent.__new__(agent_mod.Agent)
        ag.browser_session = None
        ag.cfg = Config()
        ag.events = types.SimpleNamespace(info=lambda m: None,
                                          warn=lambda m: warnings.append(m))
        ag._ensure_browser_session()
    finally:
        sys.modules.pop("glmcode.browser_session", None)
    assert warnings == []


# --------------------------------------------------------------------- #
# Finding the browsers, and letting someone verify before committing

def test_the_panel_can_listen_before_the_switch_is_on():
    """The install sheet used to say "Waiting for the extension..." forever for
    anyone who had not flipped the switch first -- there was nothing to wait
    on, because the port only opened once the feature was already enabled.
    Verifying the install BEFORE handing over a logged-in browser is the right
    order."""
    cfg = Config()
    assert bx.status(cfg)["port"] is None            # off, and nothing opened
    st = bx.status(cfg, listen=True)
    assert st["port"] and st["enabled"] is False


def test_browser_detection_returns_names_and_paths():
    from glmcode import installed_browsers
    for b in installed_browsers.find():
        assert b["name"] and b["path"]
        assert pathlib.Path(b["path"]).exists()


def test_opening_a_browser_that_is_not_there_says_so():
    from glmcode import installed_browsers
    ok, err = installed_browsers.open_browser("/nope/not/a/browser")
    assert ok is False and "isn't where it was" in err




# --------------------------------------------------------------------- #
# Tabs. Reported: "shouldn't it first get a list of the open tabs, so it can
# choose, or choose to open a new tab? It's just weird as it is right now."
#
# It was: the agent silently acted on whatever tab happened to be in front,
# which in someone's own browser is rarely what the goal is about.

TABS = [
    {"id": 7, "title": "Inbox (12)", "url": "https://mail.example.com/", "active": True, "usable": True},
    {"id": 8, "title": "Pull requests", "url": "https://github.com/a/b/pulls", "active": False, "usable": True},
    {"id": 9, "title": "Extensions", "url": "chrome://extensions", "active": False, "usable": False},
]


@pytest.fixture
def tabbed(wired):
    sess, ext = wired
    ext.handle("tabs", lambda p: TABS)
    ext.handle("select_tab", lambda p: next(t for t in TABS if t["id"] == p["id"]))
    ext.handle("new_tab", lambda p: {"id": 42, "title": "New Tab", "url": p.get("url", ""),
                                     "active": True, "usable": True})
    return sess, ext


def test_the_agent_can_see_the_tabs(tabbed):
    sess, _ = tabbed
    out = sess.list_tabs()
    assert "[7] Inbox (12)" in out and "[8] Pull requests" in out
    assert "<- currently driving" in out


def test_a_browser_page_is_flagged_as_untouchable(tabbed):
    """Chrome forbids extensions from touching chrome:// pages. Saying so in
    the list beats letting the model pick one and get an error."""
    assert "extensions cannot touch this one" in tabbed[0].list_tabs()


def test_switching_tab_returns_a_snapshot_of_the_new_one(tabbed):
    sess, _ = tabbed
    out = sess.select_tab(8)
    assert "Now driving [8] Pull requests" in out
    assert "Main content:" in out          # the ordinary snapshot follows


def test_opening_a_new_tab_is_a_first_class_move(tabbed):
    """Their place in a tab they were reading is not the agent's to take."""
    sess, _ = tabbed
    out = sess.open_tab("example.com")
    assert "Opened a new tab [42]" in out
    assert "Main content:" in out


def test_a_bad_tab_id_is_refused_before_it_reaches_the_browser(tabbed):
    sess, _ = tabbed
    with pytest.raises(BrowserError) as e:
        sess.select_tab("the second one")
    assert "browser_tabs" in str(e.value)


def test_tab_tools_are_offered_only_for_the_users_own_browser(wired):
    """A browser this app launched has the one page it made, so three tools
    that always answer with the page the agent is already on would be noise in
    the longest prompt in the app."""
    sess, _ = wired
    assert sess.supports_tabs is True
    from glmcode.browser_session import BrowserSession
    assert BrowserSession(launch_factory=lambda *a: (lambda: None, object())
                          ).supports_tabs is False


def test_the_prompt_tells_it_to_look_first_and_prefer_a_new_tab():
    from glmcode.prompts import BROWSER_ATTACHED_NOTE as note
    assert "browser_tabs first" in note
    assert "browser_new_tab is the default move" in note


def test_the_tab_tools_are_wired_end_to_end():
    from glmcode.tools import BROWSER_ACTION_TOOLS, BROWSER_TAB_SCHEMAS
    names = [s["function"]["name"] for s in BROWSER_TAB_SCHEMAS]
    assert names == ["browser_tabs", "browser_switch_tab", "browser_new_tab"]
    # The permission engine keys on this set; a tool missing from it is one the
    # Browser Agent cannot call at all.
    assert set(names) <= BROWSER_ACTION_TOOLS


# --------------------------------------------------------------------- #
# Reported: "the 'open in chrome' button is just launching a fresh empty
# window."

def test_we_no_longer_pretend_we_can_open_a_chrome_url():
    """No program can open another program's chrome:// page: Chrome refuses
    those URLs on the command line, a page cannot link to them, and there is no
    API. Passing one anyway got the browser to DROP it and show an empty
    window, which is what the button appeared to do."""
    from glmcode import installed_browsers
    src = pathlib.Path(installed_browsers.__file__).read_text(encoding="utf-8")
    assert 'subprocess.Popen([str(exe), "chrome://' not in src
    assert not hasattr(installed_browsers, "open_extensions_page")


def test_bringing_a_browser_forward_passes_no_url(monkeypatch, tmp_path):
    """With no argument a running browser is focused rather than handed a blank
    window -- which is the behaviour that made the URL version look broken."""
    from glmcode import installed_browsers
    exe = tmp_path / "chrome"
    exe.write_text("#!/bin/sh\n")
    seen = {}
    monkeypatch.setattr(installed_browsers.subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv))
    ok, err = installed_browsers.open_browser(str(exe))
    assert ok and seen["argv"] == [str(exe)]


def test_supports_tabs_is_answerable_before_the_session_starts():
    """The page only exists after start(). A property that said False until
    then would silently drop the tab tools for anyone who asked first."""
    from glmcode.browser_session import BrowserSession
    assert BrowserSession(bridge=object()).supports_tabs is True
