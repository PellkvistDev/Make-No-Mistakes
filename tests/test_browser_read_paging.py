"""A long page comes back in parts the agent can actually walk.

Reported: "when the browser agent reads a long page, it gets 'truncated, x
chars total' at the end. Can the agent read the rest somehow? And can it
scroll?"

No, and yes -- and they are answers to different questions.

browser_read took NO arguments at all, so the message naming exactly how many
characters were missing offered no way whatever to reach them. That is worse
than a missing answer: the model reads the first 6000 characters, does not find
the thing, and reports that the page does not contain it. read_file had the
same defect and was fixed the same way.

Scrolling is not the fix for it. inner_text("body") returns the WHOLE document
text whatever is scrolled into view, so paging is what gets you the rest.
Scrolling matters for a different thing -- a page that loads more of itself as
you go -- and it already works, via browser_key.
"""

import pytest

from glmcode.browser_session import BrowserSession
from glmcode.tools import BROWSER_AGENT_SCHEMAS


class _Page:
    """Just enough page for _op_read_text: it only calls inner_text and url."""

    def __init__(self, text):
        self._text = text

    def inner_text(self, selector):
        assert selector == "body"
        return self._text


def _session(text):
    s = BrowserSession.__new__(BrowserSession)
    s._page = _Page(text)
    s._url = lambda: "https://example.test/long"
    return s


LINES = [f"line {i:04d} " + "x" * 40 for i in range(400)]
LONG = "\n".join(LINES)


# ------------------------------------------------- it can be continued ---

def test_the_first_part_says_how_to_get_the_next():
    out = _session(LONG)._op_read_text(max_chars=2000)
    assert "offset=" in out
    assert "not shown" in out


def test_the_offset_it_names_actually_continues():
    """The number has to be usable, not decorative."""
    s = _session(LONG)
    first = s._op_read_text(max_chars=2000)
    off = int(first.split("offset=")[1].split()[0].rstrip("]").rstrip("."))
    second = s._op_read_text(max_chars=2000, offset=off)
    assert LONG[off:off + 40] in second


def test_walking_it_to_the_end_yields_the_whole_page():
    """The property that matters: nothing is unreachable."""
    s = _session(LONG)
    seen, off, guard = "", 0, 0
    while guard < 100:
        guard += 1
        out = s._op_read_text(max_chars=2000, offset=off)
        body = out.split("\n\n", 1)[1].split("\n\n... [")[0]
        seen += body
        if "offset=" not in out:
            break
        off = int(out.split("offset=")[1].split()[0].rstrip("]").rstrip("."))
    assert LONG.replace("\n", "") == seen.replace("\n", "")


def test_the_last_part_does_not_promise_more():
    s = _session(LONG)
    out = s._op_read_text(max_chars=len(LONG) + 10)
    assert "offset=" not in out
    assert "not shown" not in out


def test_a_short_page_is_unchanged():
    out = _session("hello")._op_read_text(max_chars=6000)
    assert out.endswith("hello")
    assert "offset=" not in out


# ------------------------------------------------- how it cuts -----------

def test_it_cuts_on_a_line_boundary():
    """So the model is never handed half a sentence."""
    body = _session(LONG)._op_read_text(max_chars=2000).split("\n\n", 1)[1]
    body = body.split("\n\n... [")[0]
    assert body.endswith("x"), body[-30:]
    assert all(ln in LINES for ln in body.split("\n") if ln)


def test_one_enormous_line_is_still_returned():
    """A page with no newlines must not come back empty because there was no
    boundary to cut on."""
    out = _session("y" * 20000)._op_read_text(max_chars=2000)
    body = out.split("\n\n", 1)[1].split("\n\n... [")[0]
    assert len(body) > 1000


def test_a_silly_offset_does_not_explode():
    out = _session(LONG)._op_read_text(max_chars=2000, offset=10 ** 9)
    assert "offset=" not in out
    out2 = _session(LONG)._op_read_text(max_chars=2000, offset=-5)
    assert LONG[:20] in out2


def test_a_continued_read_says_where_it_is():
    out = _session(LONG)._op_read_text(max_chars=2000, offset=2000)
    assert "of " + str(len(LONG)) in out


# ------------------------------------------------- what the model reads --

def _schema(name):
    return {s["function"]["name"]: s["function"]
            for s in BROWSER_AGENT_SCHEMAS}[name]


def test_the_tool_actually_takes_an_offset():
    """The old message named how much was missing and the tool took no
    arguments, so the model could not act on it even in principle."""
    assert "offset" in _schema("browser_read")["parameters"]["properties"]


def test_it_is_told_not_to_conclude_absence_from_one_part():
    desc = _schema("browser_read")["description"]
    assert "absent" in desc


def test_scrolling_is_named_as_scrolling():
    """PageDown was listed among "a key like Enter, Escape, PageDown, Tab",
    which never says that this IS how you scroll."""
    desc = _schema("browser_key")["description"]
    assert "SCROLL" in desc
    for key in ("PageDown", "PageUp", "End", "Home"):
        assert key in desc, key


def test_the_two_answers_are_kept_apart():
    """Scrolling does not get you more text, and the model should not waste
    turns trying it -- browser_read already returns the whole page."""
    assert "not to read more text" in _schema("browser_key")["description"]
    assert "not scrolling" in _schema("browser_read")["description"]
