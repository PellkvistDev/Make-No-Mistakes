"""The change-review card: per-file diffs, and a Revert button per file.

Second-riskiest thing in this UI after rewind -- Revert here also writes to
disk. And the content it renders is the least trustworthy in the app: a diff is
whatever is in the files of whatever project you opened.
"""

HOSTILE = (
    "diff --git a/x.html b/x.html\n"
    "--- a/x.html\n"
    "+++ b/x.html\n"
    "@@ -1 +1 @@\n"
    "-<b>old</b>\n"
    '+<img src=x onerror=alert(1)><script>alert(2)</script>\n'
)

FILES = [
    {"path": "src/app.py", "status": "M", "diff": "@@ -1 +1 @@\n-a\n+b\n"},
    {"path": "src/new.py", "status": "A", "diff": "+++ b/src/new.py\n+hello\n"},
]


def card(desktop, files):
    desktop.boot()
    return desktop.page.evaluate("""(files) => {
      const host = document.createElement('div');
      host.id = 'probe';
      host.appendChild(buildChangesCard(files));
      document.body.appendChild(host);
      return host.querySelectorAll('.change-row').length;
    }""", files)


def test_the_card_lists_every_changed_file(desktop):
    assert card(desktop, FILES) == 2
    shown = desktop.page.eval_on_selector_all(
        "#probe .change-path", "els => els.map(e => e.textContent)")
    assert shown == ["src/app.py", "src/new.py"]
    assert desktop.errors == []


def test_a_diff_cannot_smuggle_markup_out_of_the_file_it_came_from(desktop):
    """Diff text is file content from whatever project is open. It reaches the
    DOM through innerHTML (colorDiff adds the +/- colouring), so the escaping
    has to happen first -- and it does. Worth a test because the day someone
    reorders those two steps, nothing else would notice."""
    card(desktop, [{"path": "x.html", "status": "M", "diff": HOSTILE}])
    found = desktop.page.evaluate("""() => {
      const d = document.querySelector('#probe .change-diff');
      return { imgs: d.querySelectorAll('img').length,
               scripts: d.querySelectorAll('script').length,
               bolds: d.querySelectorAll('b').length,
               text: d.textContent };
    }""")
    assert found["imgs"] == 0, "an <img> from a diff became a real element"
    assert found["scripts"] == 0, "a <script> from a diff became a real element"
    assert found["bolds"] == 0
    assert "onerror=alert(1)" in found["text"], "the diff should still be readable, as text"
    assert desktop.errors == []


def test_a_path_is_shown_as_text_not_markup(desktop):
    card(desktop, [{"path": "<b>evil</b>.py", "status": "M", "diff": "@@\n"}])
    found = desktop.page.evaluate("""() => {
      const p = document.querySelector('#probe .change-path');
      return { bolds: p.querySelectorAll('b').length, text: p.textContent };
    }""")
    assert found["bolds"] == 0
    assert found["text"] == "<b>evil</b>.py"
    assert desktop.errors == []


def test_declining_the_confirmation_does_not_revert(desktop):
    """Revert writes to disk. It must not fire on a dismissed dialog."""
    card(desktop, FILES)
    desktop.page.on("dialog", lambda d: d.dismiss())
    desktop.page.eval_on_selector("#probe .change-revert", "el => el.click()")
    desktop.page.wait_for_timeout(300)
    assert desktop.calls("revert_change") == [], "reverted despite the user saying no"
    assert desktop.errors == []


def test_accepting_the_confirmation_reverts_that_file_only(desktop):
    card(desktop, FILES)
    desktop.page.on("dialog", lambda d: d.accept())
    desktop.page.eval_on_selector("#probe .change-revert", "el => el.click()")
    desktop.page.wait_for_timeout(400)
    calls = desktop.calls("revert_change")
    assert len(calls) == 1, f"expected one revert, got {calls}"
    assert calls[0]["args"] == ["src/app.py"], f"reverted the wrong file: {calls[0]}"
    assert desktop.errors == []


def test_a_newer_turn_retires_the_older_revert_buttons(desktop):
    """The revert baseline is the pre-turn snapshot, so once another turn has
    run an older card's Revert no longer means what its label says."""
    card(desktop, FILES)
    desktop.page.evaluate("() => retireOldChangeCards()")
    state = desktop.page.eval_on_selector_all(
        "#probe .change-revert", "els => els.map(e => ({ off: e.disabled, why: e.title }))")
    assert all(s["off"] for s in state), f"stale revert buttons left live: {state}"
    assert all("newer turn" in s["why"] for s in state)
    assert desktop.errors == []
