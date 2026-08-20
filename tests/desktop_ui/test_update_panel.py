"""The Update button in Settings → General.

An update is a `git pull` and a restart, and both halves fail in ways that are
invisible from a button. So the panel is two steps: look first, and only then
offer to do it -- with every refusal saying what the actual state is, because
"couldn't update" leaves someone with a button that does not work and no idea
why.
"""

from .conftest import DEFAULT_SETTINGS


def _open_general(desktop, **replies):
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(250)
    desktop.page.click('.settings-tab-btn[data-tab="general"]')
    desktop.page.wait_for_timeout(300)
    return desktop


def test_up_to_date_offers_nothing_to_press(desktop):
    _open_general(desktop, update_check={"ok": True, "behind": 0, "branch": "main",
                                         "reason": "Already up to date."})
    assert "Up to date" in desktop.page.inner_text("#update-status")
    assert desktop.page.inner_text("#update-btn").strip() == "Check again"


def test_an_available_update_says_how_many_and_what_happens(desktop):
    """"The app will restart itself" is the part worth knowing BEFORE clicking,
    not after the window disappears."""
    _open_general(desktop, update_check={
        "ok": True, "behind": 3, "branch": "main",
        "changes": ["abc123 fix the login bug", "def456 add dark mode"]})
    said = desktop.page.inner_text("#update-status")
    assert "3 updates available" in said and "restart itself" in said
    assert "Update & restart" in desktop.page.inner_text("#update-btn")


def test_it_shows_what_is_in_the_update(desktop):
    _open_general(desktop, update_check={
        "ok": True, "behind": 2, "branch": "main",
        "changes": ["abc123 fix the login bug", "def456 add dark mode"]})
    assert desktop.page.eval_on_selector("#update-log-row", "e => !e.hidden")
    # text_content, not inner_text: the <details> is collapsed until clicked,
    # and inner_text reports nothing for content inside a closed one.
    assert "fix the login bug" in desktop.page.text_content("#update-log")


def test_a_refusal_says_what_the_actual_state_is(desktop):
    """Local edits are the common one, and the person editing their own copy is
    exactly the person most likely to press this."""
    _open_general(desktop, update_check={
        "ok": False, "dirty": 2,
        "reason": "There are 2 uncommitted changes here. Updating could "
                  "overwrite them, so it's refused."})
    assert "2 uncommitted changes" in desktop.page.inner_text("#update-status")


def test_looking_does_not_update(desktop):
    """Opening the panel checks; it must never pull as a side effect of being
    looked at."""
    _open_general(desktop, update_check={"ok": True, "behind": 5, "branch": "main"})
    assert desktop.calls("update_apply") == []
    assert len(desktop.calls("update_check")) == 1


def test_pressing_it_updates_and_says_it_is_restarting(desktop):
    _open_general(desktop,
                  update_check={"ok": True, "behind": 1, "branch": "main"},
                  update_apply={"ok": True, "updated": True, "restarted": True,
                                "count": 1})
    desktop.page.click("#update-btn")
    desktop.page.wait_for_timeout(300)
    assert desktop.calls("update_apply")
    assert "restarting" in desktop.page.inner_text("#update-status")


def test_a_failed_update_comes_back_readable_rather_than_stuck_on_updating(desktop):
    _open_general(desktop,
                  update_check={"ok": True, "behind": 1, "branch": "main"},
                  update_apply={"ok": False,
                                "reason": "The update wouldn't apply cleanly. "
                                          "Nothing was changed."})
    desktop.page.click("#update-btn")
    desktop.page.wait_for_timeout(300)
    said = desktop.page.inner_text("#update-status")
    assert "wouldn't apply cleanly" in said
    assert "Updating" not in said
    assert desktop.page.eval_on_selector("#update-btn", "e => !e.disabled")


def test_it_refuses_while_a_turn_is_running(desktop):
    """Restarting mid-turn loses whatever the agent was part-way through."""
    _open_general(desktop,
                  update_check={"ok": True, "behind": 1, "branch": "main"},
                  update_apply={"ok": False,
                                "reason": "'Fixing login' is still working. "
                                          "Let it finish, then update."})
    desktop.page.click("#update-btn")
    desktop.page.wait_for_timeout(300)
    assert "still working" in desktop.page.inner_text("#update-status")
