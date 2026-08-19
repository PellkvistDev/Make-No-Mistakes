"""Attaching to a browser the user is ALREADY running, instead of launching one.

The whole point of the existing setup is that the agent gets a browser of its
own -- its own profile, nothing of the user's in it. This is the opt-in
opposite: the agent drives the window in front of the user, signed in as them.
That is more useful and much more dangerous, so what these tests pin is the
handful of properties that make it survivable:

  - it is OFF unless someone turns it on, and the launch path is untouched;
  - teardown DISCONNECTS and never closes the user's browser;
  - the page picked is the one they are looking at, not pages[0];
  - the viewport quoted to the model is the real window's, not a stale default;
  - the endpoint has to be this machine;
  - the Browser Agent is told whose browser it is.
"""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.browser_session import (BrowserError, BrowserSession,  # noqa: E402
                                     _attached_page)


# --------------------------------------------------------------------- #
# Fakes: a browser with several tabs, one of which is the visible one.

class FakeTab:
    def __init__(self, name, visibility="hidden", closed=False):
        self.name = name
        self.visibility = visibility
        self._closed = closed
        self.viewport_size = None

    def is_closed(self):
        return self._closed

    def evaluate(self, js, *a):
        if "visibilityState" in js:
            return self.visibility
        raise AssertionError("unexpected evaluate: " + js)


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.closed = False
        self.made = []

    def close(self):
        self.closed = True

    def new_page(self):
        tab = FakeTab("new-tab", "visible")
        self.made.append(tab)
        self.pages.append(tab)
        return tab


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


# --------------------------------------------------------------------- #
# Which tab gets driven

def test_it_drives_the_tab_the_user_is_looking_at():
    """context.pages is creation order, so pages[0] is whatever they opened
    first -- almost never the window in front of them. visibilityState is the
    page telling us itself, which is a fact rather than a guess."""
    a, b, c = FakeTab("first"), FakeTab("front", "visible"), FakeTab("third")
    browser = FakeBrowser([FakeContext([a, b, c])])
    assert _attached_page(browser) is b


def test_it_looks_across_every_window():
    """A second window (or an incognito one) is a separate context. Searching
    only contexts[0] would drive a tab in the wrong window."""
    bg = FakeContext([FakeTab("bg")])
    front = FakeContext([FakeTab("front", "visible")])
    assert _attached_page(FakeBrowser([bg, front])).name == "front"


def test_with_nothing_visible_it_takes_the_newest_tab():
    """Every tab can report hidden -- the whole browser minimised, or a page
    that will not answer. Falling back to the most recent beats failing."""
    ctx = FakeContext([FakeTab("old"), FakeTab("newest")])
    assert _attached_page(FakeBrowser([ctx])).name == "newest"


def test_a_page_that_will_not_answer_is_skipped_not_fatal():
    class Mute(FakeTab):
        def evaluate(self, js, *a):
            raise RuntimeError("detached frame")

    ctx = FakeContext([Mute("broken"), FakeTab("front", "visible")])
    assert _attached_page(FakeBrowser([ctx])).name == "front"


def test_closed_tabs_are_ignored():
    ctx = FakeContext([FakeTab("gone", "visible", closed=True), FakeTab("real")])
    assert _attached_page(FakeBrowser([ctx])).name == "real"


def test_an_empty_browser_gets_a_new_tab():
    ctx = FakeContext([])
    assert _attached_page(FakeBrowser([ctx])) is ctx.made[0]


# --------------------------------------------------------------------- #
# The session

def attached(page=None, **kw):
    """A session wired to a fake attach, plus what teardown did."""
    page = page or FakeTab("front", "visible")
    state = {"torn": False}

    def attach(connect_url, viewport):
        state["url"] = connect_url
        state["viewport"] = viewport
        return (lambda: state.__setitem__("torn", True)), page

    sess = BrowserSession(connect_url="http://localhost:9222",
                          attach_factory=attach, **kw)
    return sess, page, state


def test_off_by_default_and_the_launch_path_is_untouched():
    """The dangerous mode has to be asked for. A session with no connect_url
    must go down exactly the path it always did."""
    used = {}

    def launch(headless, executable_path, viewport, user_data_dir):
        used["launched"] = True
        return (lambda: None), FakeTab("launched")

    sess = BrowserSession(launch_factory=launch)
    assert sess.is_attached is False
    sess.start()
    assert used.get("launched") is True
    sess.close()


def test_attaching_never_launches():
    launched = []
    sess, _, state = attached(
        launch_factory=lambda *a: launched.append(a) or ((lambda: None), None))
    sess.start()
    assert sess.is_attached is True
    assert launched == [], "attaching must not launch a browser as well"
    assert state["url"] == "http://localhost:9222"
    sess.close()


def test_teardown_disconnects_and_leaves_the_browser_alone():
    """The one thing this feature must never do. `context.close()` or
    `browser.close()` would reach across the connection and shut windows the
    user has open; only the driver goes away."""
    sess, _, state = attached()
    sess.start()
    sess.close()
    assert state["torn"] is True


def test_the_real_attacher_closes_nothing():
    """Belt and braces on the above, against the actual teardown the app uses
    rather than a fake of it -- this is the assertion that would catch someone
    'tidying up' _real_attach with a browser.close()."""
    import glmcode.browser_session as bs
    ctx = FakeContext([FakeTab("front", "visible")])
    browser = FakeBrowser([ctx])
    stopped = {"pw": False}

    class FakePW:
        def stop(self):
            stopped["pw"] = True

        @property
        def chromium(self):
            return types.SimpleNamespace(
                connect_over_cdp=lambda url, timeout=0: browser)

    pw = FakePW()
    fake_api = types.ModuleType("playwright.sync_api")
    fake_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: pw)
    sys.modules["playwright.sync_api"] = fake_api
    try:
        teardown, page = bs._real_attach("http://localhost:9222", (1280, 800))
        assert page.name == "front"
        teardown()
    finally:
        sys.modules.pop("playwright.sync_api", None)
    assert stopped["pw"] is True
    assert browser.closed is False, "teardown closed the user's browser"
    assert ctx.closed is False, "teardown closed the user's window"


def test_a_failed_connect_says_how_to_open_the_port():
    """'Connection refused' on its own is useless: the port cannot be turned on
    for a browser that is already running, and that is the thing to know."""
    import glmcode.browser_session as bs
    fake_api = types.ModuleType("playwright.sync_api")

    class FakePW:
        def stop(self):
            pass

        @property
        def chromium(self):
            def boom(url, timeout=0):
                raise RuntimeError("ECONNREFUSED")
            return types.SimpleNamespace(connect_over_cdp=boom)

    fake_api.sync_playwright = lambda: types.SimpleNamespace(start=lambda: FakePW())
    sys.modules["playwright.sync_api"] = fake_api
    try:
        with pytest.raises(BrowserError) as e:
            bs._real_attach("http://localhost:9222", (1280, 800))
    finally:
        sys.modules.pop("playwright.sync_api", None)
    msg = str(e.value)
    assert "--remote-debugging-port" in msg
    assert "--user-data-dir" in msg


# --------------------------------------------------------------------- #
# The viewport the model is told about

def test_it_adopts_the_real_window_size():
    """Every snapshot header quotes self.viewport and browser_click_at REFUSES
    coordinates outside it. An attached window is whatever size the user left
    it, so a stale 1280x800 would describe the page wrongly and then reject the
    right click for being out of bounds."""
    page = FakeTab("front", "visible")
    page.viewport_size = {"width": 1600, "height": 1000}
    sess, _, _ = attached(page)
    sess.start()
    assert sess.viewport == (1600, 1000)
    sess.close()


def test_it_asks_the_page_when_playwright_has_no_size():
    """connect_over_cdp contexts commonly report viewport_size None -- they did
    not set one, the real window did."""
    class Sized(FakeTab):
        def evaluate(self, js, *a):
            if "innerWidth" in js:
                return {"width": 900, "height": 640}
            return super().evaluate(js, *a)

    sess, _, _ = attached(Sized("front", "visible"))
    sess.start()
    assert sess.viewport == (900, 640)
    sess.close()


def test_a_page_that_reports_nothing_keeps_the_default():
    """Best-effort: an unreadable size must not fail the attach."""
    class Mute(FakeTab):
        def evaluate(self, js, *a):
            raise RuntimeError("nope")

    sess, _, _ = attached(Mute("front"))
    sess.start()
    assert sess.viewport == (1280, 800)
    sess.close()


# --------------------------------------------------------------------- #
# The setting

from glmcode.gui import app as gui_app  # noqa: E402
from glmcode.config import Config  # noqa: E402


def _api(monkeypatch):
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = Config()
    api._chats = {}
    api.session_id = None          # set_setting answers with _settings()
    monkeypatch.setattr(gui_app, "save_config", lambda cfg: None)
    return api


@pytest.mark.parametrize("typed,stored", [
    ("", ""),
    ("   ", ""),
    ("localhost:9222", "http://localhost:9222"),        # scheme filled in
    ("http://localhost:9222", "http://localhost:9222"),
    ("127.0.0.1:9222", "http://127.0.0.1:9222"),
    ("http://[::1]:9222", "http://[::1]:9222"),
])
def test_endpoints_that_are_accepted(monkeypatch, typed, stored):
    api = _api(monkeypatch)
    res = api.set_setting("browser_connect_url", typed)
    assert "error" not in res
    assert api._cfg.browser_connect_url == stored


@pytest.mark.parametrize("typed,says", [
    ("http://example.com:9222", "isn't this machine"),
    ("http://localhost", "debugging port"),
    ("ftp://localhost:9222", "http://"),
    ("not a url", "isn't an address"),
])
def test_endpoints_that_are_refused(monkeypatch, typed, says):
    """A typo that silently did nothing would look like the feature being
    broken, and a hostname that is not this machine is a mistake worth
    refusing rather than obeying -- it would aim the agent at someone else's
    signed-in browser."""
    api = _api(monkeypatch)
    res = api.set_setting("browser_connect_url", typed)
    assert says in res.get("error", "")
    assert api._cfg.browser_connect_url == ""   # nothing stored on a refusal


def test_it_can_be_turned_back_off(monkeypatch):
    api = _api(monkeypatch)
    api.set_setting("browser_connect_url", "localhost:9222")
    api.set_setting("browser_connect_url", "")
    assert api._cfg.browser_connect_url == ""


def test_the_setting_reaches_the_ui(monkeypatch):
    api = _api(monkeypatch)
    api._cfg.browser_connect_url = "http://localhost:9222"
    assert api._settings()["browser_connect_url"] == "http://localhost:9222"


def test_the_check_refuses_the_same_addresses(monkeypatch):
    api = _api(monkeypatch)
    res = api.browser_attach_check("http://example.com:9222")
    assert "isn't this machine" in res["error"]
    assert "--remote-debugging-port" in res["hint"]


# --------------------------------------------------------------------- #
# What the agent does with it

def test_the_agent_attaches_when_configured(monkeypatch):
    """The config flag has to reach BrowserSession, and the launch-only
    arguments must NOT: headless and the saved-profile directory describe a
    browser we start, and this one is already running."""
    import glmcode.agent as agent_mod
    built = {}

    class FakeSession:
        def __init__(self, **kw):
            built.update(kw)
            self.is_open = True

        def start(self):
            pass

    fake_mod = types.ModuleType("glmcode.browser_session")
    fake_mod.BrowserSession = FakeSession
    monkeypatch.setitem(sys.modules, "glmcode.browser_session", fake_mod)

    ag = agent_mod.Agent.__new__(agent_mod.Agent)
    ag.browser_session = None
    ag.cfg = Config()
    ag.cfg.browser_connect_url = "http://localhost:9222"
    ag.cfg.browser_headless = True
    ag.cfg.browser_keep_logins = True
    ag.events = types.SimpleNamespace(info=lambda m: None)

    ag._ensure_browser_session()
    assert built["connect_url"] == "http://localhost:9222"
    assert "headless" not in built
    assert "user_data_dir" not in built


def test_the_agent_launches_when_not_configured(monkeypatch):
    import glmcode.agent as agent_mod
    built = {}

    class FakeSession:
        def __init__(self, **kw):
            built.update(kw)
            self.is_open = True

        def start(self):
            pass

    fake_mod = types.ModuleType("glmcode.browser_session")
    fake_mod.BrowserSession = FakeSession
    monkeypatch.setitem(sys.modules, "glmcode.browser_session", fake_mod)

    ag = agent_mod.Agent.__new__(agent_mod.Agent)
    ag.browser_session = None
    ag.cfg = Config()
    ag.cfg.browser_headless = True
    ag.events = types.SimpleNamespace(info=lambda m: None)

    ag._ensure_browser_session()
    assert built.get("connect_url") is None
    assert built["headless"] is True


def test_the_browser_agent_is_told_whose_browser_it_is():
    """A model that believes it is in a throwaway profile will click 'Sign out'
    to get a clean login form, or empty a cart to start over. Reasonable in a
    sandbox; destructive in the window someone lives in. The note is in the
    SYSTEM prompt because it has to hold over every improvised action, not
    just the first one."""
    from glmcode.prompts import BROWSER_AGENT_SYSTEM, BROWSER_ATTACHED_NOTE
    system = BROWSER_AGENT_SYSTEM.format(goal="buy milk") + BROWSER_ATTACHED_NOTE
    assert "not a sandbox" in system.lower()
    for word in ("sign out", "irreversible", "Leave the browser as you found it"):
        assert word.lower() in system.lower(), word
    # And it survives .format() being applied to the prompt it is appended to.
    assert "{" not in BROWSER_ATTACHED_NOTE
