"""BrowserSession drives Playwright from a dedicated thread and marshals every
command onto it. These tests exercise all of that routing -- snapshot ref
tracking, action dispatch, error handling, lifecycle -- with a FAKE page, so
no real Chromium (or display) is needed."""

import threading

import pytest

from glmcode.browser_session import BrowserError, BrowserSession


class FakeHandle:
    def __init__(self, tag, attrs=None, text="", visible=True):
        self._tag = tag
        self._attrs = attrs or {}
        self._text = text
        self._visible = visible
        self.clicked = 0
        self.filled = None
        self.pressed = []

    def is_visible(self):
        return self._visible

    def evaluate(self, _js):
        return self._tag.upper()

    def get_attribute(self, name):
        return self._attrs.get(name)

    def text_content(self):
        return self._text

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def click(self, timeout=0):
        self.clicked += 1

    def fill(self, text, timeout=0):
        self.filled = text

    def press(self, key):
        self.pressed.append(key)


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self._title = "Blank"
        self.handles = []
        self.goto_calls = []
        self.back_calls = 0
        self.body_text = ""
        self.key_presses = []
        self.screens = []
        self.thread_ids = set()  # every op should run on the SAME driver thread

    def _record_thread(self):
        self.thread_ids.add(threading.get_ident())

    def goto(self, url, wait_until=None, timeout=0):
        self._record_thread()
        self.goto_calls.append(url)
        self.url = url
        self._title = "Example Domain"
        self.handles = [
            FakeHandle("input", {"name": "q", "placeholder": "Search"}),
            FakeHandle("a", text="More information"),
            FakeHandle("button", {"aria-label": "Go"}),
            FakeHandle("a", text="hidden link", visible=False),
        ]

    def go_back(self, wait_until=None, timeout=0):
        self._record_thread()
        self.back_calls += 1
        self.url = "about:blank"

    def query_selector_all(self, _sel):
        self._record_thread()
        return self.handles

    def inner_text(self, _sel):
        self._record_thread()
        return self.body_text

    def title(self):
        return self._title

    def screenshot(self, path=None):
        self._record_thread()
        self.screens.append(path)

    def wait_for_timeout(self, _ms):
        pass

    @property
    def keyboard(self):
        page = self

        class _KB:
            def press(self, key):
                page._record_thread()
                page.key_presses.append(key)
        return _KB()


def make_session(**kw):
    page = FakePage()
    torn = {"down": False}
    seen = {}

    def factory(headless, executable_path, viewport, user_data_dir):
        seen["user_data_dir"] = user_data_dir
        return (lambda: torn.__setitem__("down", True)), page

    sess = BrowserSession(launch_factory=factory, **kw)
    sess._test_seen = seen
    return sess, page, torn


def test_navigate_returns_numbered_snapshot():
    sess, page, _ = make_session()
    snap = sess.navigate("example.com")
    assert page.goto_calls == ["https://example.com"]   # bare host gets https
    # visible interactive elements are numbered; the hidden one is skipped
    assert "[1] input" in snap and "Search" in snap
    assert "[2] a" in snap and "More information" in snap
    assert "[3] button" in snap and "Go" in snap
    assert "hidden link" not in snap
    assert "Interactive elements (3)" in snap
    sess.close()


def test_click_uses_the_ref_from_the_last_snapshot():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    sess.click(2)
    assert page.handles[1].clicked == 1     # ref [2] -> second handle
    sess.close()


def test_type_text_fills_and_optionally_submits():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    sess.type_text(1, "hello", submit=True)
    assert page.handles[0].filled == "hello"
    assert page.handles[0].pressed == ["Enter"]
    sess.close()


def test_stale_ref_gives_a_helpful_error():
    sess, _, _ = make_session()
    sess.navigate("example.com")
    with pytest.raises(BrowserError) as ei:
        sess.click(99)
    assert "re-snapshot" in str(ei.value) or "snapshot again" in str(ei.value)
    sess.close()


def test_read_text_truncates():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.body_text = "x" * 10_000
    out = sess.read_text(max_chars=500)
    assert "truncated" in out and out.count("x") <= 520
    sess.close()


def test_press_and_go_back_and_screenshot(tmp_path):
    sess, page, _ = make_session()
    sess.navigate("example.com")
    sess.press("Escape")
    assert page.key_presses == ["Escape"]
    sess.go_back()
    assert page.back_calls == 1
    p = sess.screenshot(tmp_path / "shot.png")
    assert page.screens == [str(tmp_path / "shot.png")]
    assert p.endswith("shot.png")
    sess.close()


def test_every_op_runs_on_one_dedicated_driver_thread():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    # Drive commands from several different caller threads...
    def worker():
        sess.snapshot()
        sess.read_text()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    # ...yet Playwright was only ever touched from a single thread (the driver),
    # and never from any caller thread.
    assert len(page.thread_ids) == 1
    assert threading.get_ident() not in page.thread_ids
    sess.close()


def test_close_tears_down_and_blocks_further_use():
    sess, _, torn = make_session()
    sess.navigate("example.com")
    sess.close()
    assert torn["down"] is True
    assert sess.is_open is False
    with pytest.raises(BrowserError):
        sess.snapshot()


def test_launch_failure_surfaces_on_start():
    def boom(headless, executable_path, viewport, user_data_dir):
        raise RuntimeError("no chromium here")
    sess = BrowserSession(launch_factory=boom)
    with pytest.raises(RuntimeError, match="no chromium"):
        sess.navigate("example.com")


def test_user_data_dir_reaches_the_launcher():
    # Persistent-profile mode: the profile dir must flow to the launcher;
    # default stays None (throwaway profile).
    sess, _, _ = make_session(user_data_dir="/tmp/agent-profile")
    sess.navigate("example.com")
    assert sess._test_seen["user_data_dir"] == "/tmp/agent-profile"
    sess.close()
    sess2, _, _ = make_session()
    sess2.navigate("example.com")
    assert sess2._test_seen["user_data_dir"] is None
    sess2.close()
