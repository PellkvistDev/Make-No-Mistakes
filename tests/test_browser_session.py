"""BrowserSession drives Playwright from a dedicated thread and marshals every
command onto it. These tests exercise all of that routing -- the one-pass
snapshot with STABLE stamped refs, region grouping, action dispatch with
locate-at-action-time, error handling, lifecycle -- with a FAKE page, so no
real Chromium (or display) is needed."""

import re
import threading

import pytest

from glmcode.browser_session import BrowserError, BrowserSession


class FakeHandle:
    """One interactive element: snapshot data + action surface."""

    def __init__(self, tag, attrs=None, text="", visible=True, enabled=True,
                 attached=True, region="main", value=None, options=None,
                 checked=False):
        self._tag = tag
        self._attrs = attrs or {}
        self._text = text
        self._visible = visible
        self._enabled = enabled
        self._attached = attached
        self.region = region
        self.value = value
        self.options = options
        self.checked = checked
        self.ref = None          # data-mnm-ref stamp (assigned on snapshot)
        self.clicked = 0
        self.filled = None
        self.pressed = []
        self.selected = None

    def label(self):
        for a in ("aria-label", "placeholder", "name", "alt", "title"):
            v = self._attrs.get(a)
            if v:
                return v
        return self._text or ""

    # -- action surface (used after query_selector resolves the stamp) ---- #
    def is_enabled(self):
        return self._enabled

    def scroll_into_view_if_needed(self, timeout=0):
        pass

    def click(self, timeout=0):
        self.clicked += 1

    def fill(self, text, timeout=0):
        self.filled = text

    def press(self, key):
        self.pressed.append(key)

    def select_option(self, label=None, value=None):
        want = label if label is not None else value
        if not self.options or want not in self.options:
            raise RuntimeError(f"no option {want!r}")
        self.selected = want


class FakePage:
    """Models the page contract the driver relies on: one evaluate() call
    returns the whole snapshot (emulating the data-mnm-ref stamping), and
    query_selector('[data-mnm-ref=\"N\"]') resolves a stamp to a handle."""

    def __init__(self):
        self.url = "about:blank"
        self._title = "Blank"
        self.handles = []
        self.outline = []
        self.goto_calls = []
        self.back_calls = 0
        self.body_text = ""
        self.key_presses = []
        self.screens = []
        self._next_ref = 1
        self.thread_ids = set()  # every op should run on the SAME driver thread
        self.mouse_clicks = []

    def _record_thread(self):
        self.thread_ids.add(threading.get_ident())

    def goto(self, url, wait_until=None, timeout=0):
        self._record_thread()
        self.goto_calls.append(url)
        self.url = url
        self._title = "Example Domain"
        # A navigation is a fresh JS world: stamps and the counter reset.
        self._next_ref = 1
        self.handles = [
            FakeHandle("input", {"name": "q", "placeholder": "Search"}),
            FakeHandle("a", text="More information"),
            FakeHandle("button", {"aria-label": "Go"}),
            FakeHandle("a", text="hidden link", visible=False),
        ]
        self.outline = []

    def go_back(self, wait_until=None, timeout=0):
        self._record_thread()
        self.back_calls += 1
        self.url = "about:blank"

    def evaluate(self, js, arg=None):
        self._record_thread()
        assert "mnmNextRef" in js, "unexpected evaluate"
        items, seen = [], set()
        for el in self.handles:
            if not el._visible:
                continue
            if not el.ref or el.ref in seen:
                el.ref = self._next_ref
                self._next_ref += 1
            seen.add(el.ref)
            item = {"ref": el.ref, "tag": el._tag, "label": el.label(),
                    "region": el.region, "disabled": not el._enabled,
                    "type": (el._attrs.get("type") or "").lower()}
            if el.value is not None:
                item["value"] = el.value
            if el.options is not None:
                item["options"] = list(el.options)
            if el.checked:
                item["checked"] = True
            items.append(item)
        return {"items": items, "outline": list(self.outline)}

    def query_selector(self, sel):
        self._record_thread()
        m = re.search(r'\[data-mnm-ref="(\d+)"\]', sel)
        assert m, sel
        ref = int(m.group(1))
        for el in self.handles:
            if el.ref == ref and el._attached:
                return el
        return None

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

    @property
    def mouse(self):
        page = self

        class _Mouse:
            def click(self, x, y):
                page._record_thread()
                page.mouse_clicks.append((x, y))
        return _Mouse()


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


def test_navigate_returns_grouped_numbered_snapshot():
    sess, page, _ = make_session()
    snap = sess.navigate("example.com")
    assert page.goto_calls == ["https://example.com"]   # bare host gets https
    assert "Main content:" in snap
    assert '[1] input "Search"' in snap
    assert '[2] a "More information"' in snap
    assert '[3] button "Go"' in snap
    assert "hidden link" not in snap
    sess.close()


def test_refs_are_stable_across_snapshots():
    """The #1 wrong-button cause: refs used to renumber every snapshot, so a
    remembered number silently pointed at a different element. Now refs are
    stamped into the DOM and persist; new elements get NEW numbers."""
    sess, page, _ = make_session()
    sess.navigate("example.com")
    # The page mutates: a cookie banner injects a button at the TOP of the DOM.
    page.handles.insert(0, FakeHandle("button", text="Accept all", region="dialog"))
    snap2 = sess.snapshot()
    # Existing elements keep their original numbers despite the reordering...
    assert '[1] input "Search"' in snap2
    assert '[3] button "Go"' in snap2
    # ...and the newcomer gets the next fresh number, not somebody else's.
    assert '[4] button "Accept all"' in snap2
    # A remembered ref still hits the RIGHT element after the DOM changed.
    sess.click(3)
    assert page.handles[3].clicked == 1     # "Go" (now 4th in DOM order)
    sess.close()


def test_dialog_region_is_listed_first_with_warning():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles.append(FakeHandle("button", text="Accept cookies", region="dialog"))
    snap = sess.snapshot()
    assert "OPEN DIALOG / POPUP" in snap and "deal with this first" in snap
    assert snap.index("Accept cookies") < snap.index('input "Search"')
    sess.close()


def test_input_values_and_duplicates_are_shown():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles[0].value = "laptops"
    page.handles.append(FakeHandle("button", text="Add to cart"))
    page.handles.append(FakeHandle("button", text="Add to cart"))
    snap = sess.snapshot()
    assert '[1] input "Search" = "laptops"' in snap
    assert snap.count("(one of 2 with this label)") == 2
    sess.close()


def test_outline_is_shown_when_present():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.outline = ["Checkout", "Payment details"]
    snap = sess.snapshot()
    assert "Page sections: Checkout | Payment details" in snap
    sess.close()


def test_click_uses_the_ref_from_the_snapshot():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    sess.click(2)
    assert page.handles[1].clicked == 1     # ref [2] -> second handle
    sess.close()


def test_snapshot_shows_the_viewport_size():
    sess, page, _ = make_session(viewport=(1024, 640))
    snap = sess.navigate("example.com")
    assert "Viewport: 1024x640 px" in snap
    sess.close()


def test_click_at_sends_a_raw_mouse_click_at_those_coordinates():
    sess, page, _ = make_session(viewport=(1024, 640))
    sess.navigate("example.com")
    sess.click_at(300, 150)
    assert page.mouse_clicks == [(300, 150)]
    sess.close()


def test_click_at_rejects_coordinates_outside_the_viewport():
    sess, page, _ = make_session(viewport=(1024, 640))
    sess.navigate("example.com")
    with pytest.raises(BrowserError, match="outside the 1024x640 viewport"):
        sess.click_at(2000, 150)
    with pytest.raises(BrowserError, match="outside the 1024x640 viewport"):
        sess.click_at(300, -5)
    assert page.mouse_clicks == []   # never reached the page
    sess.close()


def test_click_at_rejects_non_numeric_coordinates():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    with pytest.raises(BrowserError, match="numeric"):
        sess.click_at("left button", 150)
    assert page.mouse_clicks == []
    sess.close()


def test_type_text_fills_and_optionally_submits():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    sess.type_text(1, "hello", submit=True)
    assert page.handles[0].filled == "hello"
    assert page.handles[0].pressed == ["Enter"]
    sess.close()


def test_typing_into_a_select_chooses_the_option():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles.append(FakeHandle("select", {"name": "country"},
                                   options=["Sweden", "Norway"]))
    snap = sess.snapshot()
    assert "(options: Sweden, Norway)" in snap
    ref = int(re.search(r'\[(\d+)\] select', snap).group(1))
    sess.type_text(ref, "Sweden")
    assert page.handles[-1].selected == "Sweden"
    assert page.handles[-1].filled is None   # fill() never used on a select
    # A non-existent option fails with the real options listed.
    with pytest.raises(BrowserError, match="Sweden, Norway"):
        sess.type_text(ref, "Atlantis")
    sess.close()


def test_typing_into_a_checkbox_says_click_instead():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles.append(FakeHandle("input", {"type": "checkbox", "name": "tos"}))
    snap = sess.snapshot()
    ref = int(re.search(r'\[(\d+)\] input "tos"', snap).group(1))
    with pytest.raises(BrowserError, match="browser_click"):
        sess.type_text(ref, "yes")
    sess.close()


def test_unknown_ref_gives_a_helpful_error():
    sess, _, _ = make_session()
    sess.navigate("example.com")
    with pytest.raises(BrowserError, match="browser_snapshot"):
        sess.click(99)
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
    def worker():
        sess.snapshot()
        sess.read_text()
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
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


def test_disabled_elements_are_flagged_in_the_snapshot():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles.append(FakeHandle("button", text="Submit", enabled=False))
    snap = sess.snapshot()
    assert '[4] button "Submit" (disabled)' in snap
    assert "(disabled) can't be used" in snap
    assert '[3] button "Go" (disabled)' not in snap
    sess.close()


def test_typing_into_disabled_element_fails_instantly_with_advice():
    """The exact failure from the field: fill() against a disabled input used
    to spin Playwright's 10s retry loop, then surface a useless Call-log
    dump. Now it fails immediately and tells the model what to do instead."""
    import time
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles[0]._enabled = False
    snap = sess.snapshot()
    assert "(disabled)" in snap
    t0 = time.monotonic()
    with pytest.raises(BrowserError) as ei:
        sess.type_text(1, "x=0")
    took = time.monotonic() - t0
    assert took < 1.0
    msg = str(ei.value)
    assert "disabled" in msg and "[1]" in msg
    assert "Call log" not in msg
    assert page.handles[0].filled is None
    sess.close()


def test_clicking_detached_element_says_resnapshot():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    page.handles[1]._attached = False  # the page replaced this node
    with pytest.raises(BrowserError, match="browser_snapshot"):
        sess.click(2)
    assert page.handles[1].clicked == 0
    sess.close()


def test_action_failure_message_drops_the_call_log():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    def boom(text, timeout=0):
        raise RuntimeError("Timeout 8000ms exceeded.\nCall log:\n  - attempting fill action\n  - retrying")
    page.handles[0].fill = boom
    with pytest.raises(BrowserError) as ei:
        sess.type_text(1, "hello")
    msg = str(ei.value)
    assert "Timeout 8000ms exceeded." in msg
    assert "Call log" not in msg and "retrying" not in msg
    assert "browser_snapshot" in msg
    sess.close()


def test_wait_clamps_and_returns_a_snapshot():
    sess, page, _ = make_session()
    sess.navigate("example.com")
    waited = []
    page.wait_for_timeout = lambda ms: waited.append(ms)
    out = sess.wait(3)
    assert 3000 in waited
    assert "Main content:" in out          # fresh snapshot came back
    sess.wait(9999)                        # clamped to 10s max
    assert waited[-1] == 10_000
    sess.wait("garbage")                   # bad input -> default, no crash
    assert waited[-1] == 2000
    sess.close()


def test_user_data_dir_reaches_the_launcher():
    sess, _, _ = make_session(user_data_dir="/tmp/agent-profile")
    sess.navigate("example.com")
    assert sess._test_seen["user_data_dir"] == "/tmp/agent-profile"
    sess.close()
    sess2, _, _ = make_session()
    sess2.navigate("example.com")
    assert sess2._test_seen["user_data_dir"] is None
    sess2.close()
