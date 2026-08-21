"""The agent must not resize the browser the user lives in.

Reported: taking a screenshot dropped the browser out of fullscreen.

`chrome.windows.update(id, {state: "normal"})` is not "make this window
visible" -- it is Chrome's RESTORE. It un-maximizes a maximized window and
drops a fullscreen one back to a floating rectangle. capture() called it before
every screenshot, so the agent reshaped the user's own window on the way past,
with no undo: once the state has been changed, the previous one is not readable
from anywhere.

`focused: true` alone is what was wanted. It raises a covered window, and for a
minimized one Chrome restores whatever it was before -- which is precisely the
value that would otherwise have to be guessed at.

Read from the shipped source: the failure is a call that must not be there, and
there is no Chrome here to observe it in.
"""

import pathlib
import re

EXT = pathlib.Path(__file__).resolve().parent.parent / "extension"
BG = (EXT / "background.js").read_text(encoding="utf-8")


def _updates():
    """Every chrome.windows.update call in the extension, as source text."""
    return re.findall(r"chrome\.windows\.update\([^)]*\)", BG)


def test_the_extension_never_sets_a_window_state():
    """Not just in capture(): ANY of these reshapes a window the user arranged,
    and every one of them is reachable from an ordinary agent action."""
    offenders = [c for c in _updates() if "state" in c]
    assert not offenders, offenders


def test_capture_still_raises_the_window():
    """The screenshot needs the tab rendered -- captureVisibleTab reads what is
    ON SCREEN, and fails with "image readback failed" when the window is behind
    the app. Dropping `state` must not turn into dropping the raise."""
    body = BG[BG.index("async function capture(tab)"):]
    body = body[:body.index("\n}\n")]
    assert "chrome.windows.update" in body
    assert "focused: true" in body
    assert "chrome.tabs.update(tab.id, { active: true })" in body


def test_switching_tabs_raises_without_reshaping_either():
    """select_tab brings its window forward for the same reason, and had the
    same opportunity to get this wrong."""
    body = BG[BG.index('if (command === "select_tab")'):]
    body = body[:body.index('if (command === "new_tab")')]
    assert "focused: true" in body
    assert "state" not in body


def test_a_capture_that_cannot_be_taken_says_what_to_do_instead():
    """Raising the window is best-effort. When it still cannot be photographed
    the agent has two tools that need no pixels, and being told so is the
    difference between a stuck turn and a slower one."""
    assert "browser_read" in BG
    assert "browser_snapshot" in BG
