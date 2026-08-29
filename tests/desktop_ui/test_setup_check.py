"""The setup check, in the real panel.

The backend decides what is wrong; this is about whether the answer is
readable. Two things it must not do: hide the fix, and use colour as the
message.
"""

from .conftest import DEFAULT_SETTINGS

RESULT = {
    "worst": "fail", "problems": 3,
    "checks": [
        {"id": "sync", "label": "Shared chats", "status": "fail",
         "detail": "The chat index couldn't be decrypted.",
         "fix": "Settings -> Your phone -> Rebuild the list."},
        {"id": "browser_model", "label": "Browser agent model", "status": "warn",
         "detail": "Not set, so it uses whichever model the chat is on.",
         "fix": "Set a stronger one in Settings -> Browser."},
        {"id": "credentials", "label": "Saved credentials", "status": "unknown",
         "detail": "Couldn't check: OSError: nope", "fix": ""},
        {"id": "git", "label": "git", "status": "ok",
         "detail": "Found on your PATH.", "fix": ""},
    ],
}


def _open(desktop, result=RESULT):
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)})
    desktop.reply("self_check", result)
    desktop.page.evaluate("() => { openSettings && openSettings(); }")
    desktop.page.wait_for_timeout(120)
    desktop.page.evaluate("() => document.getElementById('setup-check').click()")
    desktop.page.wait_for_timeout(250)
    return desktop


def _rows(desktop):
    return desktop.page.evaluate("""() => [...document.querySelectorAll('#setup-check-results .check-row')]
      .map((el) => ({
        cls: el.className,
        tag: el.querySelector('.check-tag').textContent,
        name: el.querySelector('.check-name').textContent,
        detail: el.querySelector('.check-detail').textContent,
        fix: (el.querySelector('.check-fix') || {}).textContent || "",
      }))""")


def test_it_asks_the_backend_and_shows_every_row(desktop):
    _open(desktop)
    assert desktop.calls("self_check"), "the button asked nothing"
    rows = _rows(desktop)
    assert [r["name"] for r in rows] == [
        "Shared chats", "Browser agent model", "Saved credentials", "git"]
    assert desktop.errors == []


def test_the_status_is_a_word_and_not_only_a_colour(desktop):
    """A coloured dot alone is a puzzle to solve before the row can be read,
    and it is nothing at all to someone who cannot tell the hues apart."""
    _open(desktop)
    tags = [r["tag"] for r in _rows(desktop)]
    assert tags == ["Broken", "Worth fixing", "Couldn't check", "OK"]


def test_the_fix_is_shown_where_there_is_one(desktop):
    """The fix is the point of the row. A diagnostic that only names what is
    wrong sends you back to the panel you already did not think to open."""
    rows = {r["name"]: r for r in _rows(_open(desktop))}
    assert "Rebuild the list" in rows["Shared chats"]["fix"]
    assert rows["git"]["fix"] == ""


def test_could_not_check_is_not_dressed_as_a_failure(desktop):
    """"I couldn't test this" is not a verdict on the thing tested."""
    rows = {r["name"]: r for r in _rows(_open(desktop))}
    assert "check-unknown" in rows["Saved credentials"]["cls"]
    assert "check-fail" not in rows["Saved credentials"]["cls"]


def test_a_clean_bill_of_health_says_so(desktop):
    _open(desktop, {"worst": "ok", "problems": 0, "checks": [
        {"id": "git", "label": "git", "status": "ok",
         "detail": "Found on your PATH.", "fix": ""}]})
    sub = desktop.page.evaluate("() => document.getElementById('setup-check-sub').textContent")
    assert "checks out" in sub
    assert desktop.errors == []


def test_a_count_is_offered_when_there_is_something_to_do(desktop):
    _open(desktop)
    sub = desktop.page.evaluate("() => document.getElementById('setup-check-sub').textContent")
    assert sub.startswith("3 things")


def test_the_status_stripe_is_actually_drawn(desktop):
    """Asserted as a COMPUTED width, not as a class name.

    The first version of this stylesheet used `border: 1px solid var(--line)`,
    and `--line` is not a token in this app. An undefined custom property makes
    the whole shorthand invalid at computed-value time, so `border-style` stays
    `none` and the width computes to 0px -- every colour below it correct, and
    nothing drawn. Every test here passed, because a class name is not a
    picture. A screenshot settled it in one look; this keeps it settled.
    """
    _open(desktop)
    widths = desktop.page.evaluate(
        """() => [...document.querySelectorAll('#setup-check-results .check-row')]
             .map((e) => getComputedStyle(e).borderLeftWidth)""")
    assert widths and all(w != "0px" for w in widths), widths


def test_each_status_is_a_different_stripe(desktop):
    """Colour is reinforcement, not the message -- but reinforcement that is
    the same for every row reinforces nothing."""
    _open(desktop)
    colours = desktop.page.evaluate(
        """() => [...document.querySelectorAll('#setup-check-results .check-row')]
             .map((e) => getComputedStyle(e).borderLeftColor)""")
    assert len(set(colours)) == len(colours), colours


def test_it_is_its_own_section_rather_than_a_heading_on_top_of_another(desktop):
    """The search names a group by its section's FIRST h3.

    So a heading added at the top of an existing section RENAMES that group:
    putting this one above Appearance made that section report as "Setup" and
    Appearance vanished from the filter. Found by
    tests/desktop_ui/test_settings_search.py rather than by looking at it.

    Sections here do carry more than one heading -- Appearance also holds
    "When the model is rate-limited" and "Update" -- so this is not a rule
    about headings per section. It is about not taking over the first one.
    """
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)})
    firsts = desktop.page.evaluate(
        """() => [...document.querySelectorAll('#settings-backdrop section[data-tab="general"]')]
             .map((s) => (s.querySelector('h3') || {}).textContent || "")""")
    assert "Setup" in firsts, firsts
    assert "Appearance" in firsts, f"Setup took over Appearance's group: {firsts}"
