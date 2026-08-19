"""The Browser settings panel, for the "use my own browser" endpoint.

This is the one browser setting that changes what the agent can REACH, so the
panel has work to do beyond storing a string: it has to say what turning it on
means, refuse a bad endpoint out loud rather than silently keeping the old one,
and let someone confirm a browser is actually listening before they rely on it
mid-task.
"""

from .conftest import DEFAULT_SETTINGS


def _open_browser_tab(desktop, settings=None, **replies):
    boot = {"settings": dict(DEFAULT_SETTINGS, **(settings or {}))}
    desktop.boot(boot=boot, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(250)
    desktop.page.click('.settings-tab-btn[data-tab="browser"]')
    desktop.page.wait_for_timeout(200)
    return desktop


def test_the_field_shows_what_is_configured(desktop):
    _open_browser_tab(desktop, {"browser_connect_url": "http://localhost:9222"})
    assert desktop.page.input_value("#opt-browser-connect") == "http://localhost:9222"
    assert desktop.errors == []


def test_empty_is_the_default_and_says_nothing_alarming(desktop):
    _open_browser_tab(desktop)
    assert desktop.page.input_value("#opt-browser-connect") == ""
    assert desktop.page.eval_on_selector(
        "#opt-browser-connect", "e => e.classList.contains('attached-on')") is False


def test_typing_an_endpoint_saves_it_once_you_leave_the_field(desktop):
    """Per-keystroke saving would walk through every prefix of the address, and
    some of those prefixes are themselves valid endpoints pointing somewhere
    else."""
    _open_browser_tab(desktop, set_setting=dict(
        DEFAULT_SETTINGS, browser_connect_url="http://localhost:9222"))
    desktop.page.fill("#opt-browser-connect", "localhost:9222")
    assert desktop.calls("set_setting") == []          # nothing yet
    desktop.page.eval_on_selector("#opt-browser-connect", "e => e.blur()")
    desktop.page.wait_for_timeout(200)
    calls = desktop.calls("set_setting")
    assert len(calls) == 1
    assert calls[0]["args"][:2] == ["browser_connect_url", "localhost:9222"]
    # The BACKEND's normalised value wins over what was typed.
    assert desktop.page.input_value("#opt-browser-connect") == "http://localhost:9222"


def test_a_refused_endpoint_says_why_and_puts_the_old_one_back(desktop):
    """Silently keeping the previous value is how you end up believing the
    agent is attached to a browser it has never heard of."""
    _open_browser_tab(desktop, {"browser_connect_url": "http://localhost:9222"},
                      set_setting={"error": "'evil.com' isn't this machine."})
    desktop.page.fill("#opt-browser-connect", "http://evil.com:9222")
    desktop.page.eval_on_selector("#opt-browser-connect", "e => e.blur()")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.input_value("#opt-browser-connect") == "http://localhost:9222"
    assert "isn't this machine" in desktop.page.inner_text("body")


def test_check_reports_which_browser_answered(desktop):
    _open_browser_tab(desktop, {"browser_connect_url": "http://localhost:9222"},
                      browser_attach_check={"ok": True, "url": "http://localhost:9222",
                                            "browser": "Chrome/140.0.0.0", "hint": "..."})
    desktop.page.click("#browser-attach-check")
    desktop.page.wait_for_timeout(250)
    status = desktop.page.inner_text("#browser-attach-status")
    assert "Chrome/140.0.0.0" in status and "listening" in status


def test_check_says_so_when_nothing_is_there(desktop):
    """The failure is the common case -- the port cannot be opened on a browser
    that is already running, so the first check usually fails and the message
    is the whole value of the button."""
    _open_browser_tab(desktop, {"browser_connect_url": "http://localhost:9222"},
                      browser_attach_check={"ok": False,
                                            "error": "Nothing answered at http://localhost:9222.",
                                            "hint": "..."})
    desktop.page.click("#browser-attach-check")
    desktop.page.wait_for_timeout(250)
    assert "Nothing answered" in desktop.page.inner_text("#browser-attach-status")
    assert desktop.page.eval_on_selector(
        "#browser-attach-status", "e => e.classList.contains('attach-bad')") is True
