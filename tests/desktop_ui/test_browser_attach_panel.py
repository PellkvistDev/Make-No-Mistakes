"""The Browser settings panel: using the browser you already have open.

Two ways to do that, and the panel's job is to make the difference obvious.

The EXTENSION is the one that works: it is already inside the browser, so there
is nothing to relaunch and the agent drives the tab you are looking at. Its
whole risk is install friction, so the sheet is tested like the install
instructions it is.

The DEVTOOLS PORT is the older route, now behind an "Advanced" disclosure. It
cannot reach a running browser at all -- the port only opens at launch -- so it
is kept for the real input events it gives, not offered as the way in.
"""

from .conftest import DEFAULT_SETTINGS


EXT_OFF = {"enabled": True, "connected": False, "port": 8765,
           "path": "/home/you/GLMCode/extension", "installed": True}
EXT_ON = dict(EXT_OFF, connected=True)
EXT_DISABLED = dict(EXT_OFF, enabled=False, port=None)
EXT_WAITING = dict(EXT_OFF)


def _open_browser_tab(desktop, settings=None, **replies):
    boot = {"settings": dict(DEFAULT_SETTINGS, **(settings or {}))}
    replies.setdefault("browser_extension_status", EXT_OFF)
    desktop.boot(boot=boot, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.wait_for_timeout(250)
    desktop.page.click('.settings-tab-btn[data-tab="browser"]')
    desktop.page.wait_for_timeout(250)
    return desktop


def _open_advanced(desktop):
    """The DevTools-port controls sit behind a disclosure now: it is the route
    that needs a relaunch, so it must not read as the way in."""
    desktop.page.eval_on_selector(
        "#opt-browser-connect", "e => e.closest('details').open = true")
    desktop.page.wait_for_timeout(120)


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
    _open_advanced(desktop)
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
    _open_advanced(desktop)
    desktop.page.fill("#opt-browser-connect", "http://evil.com:9222")
    desktop.page.eval_on_selector("#opt-browser-connect", "e => e.blur()")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.input_value("#opt-browser-connect") == "http://localhost:9222"
    assert "isn't this machine" in desktop.page.inner_text("body")


def test_check_reports_which_browser_answered(desktop):
    _open_browser_tab(desktop, {"browser_connect_url": "http://localhost:9222"},
                      browser_attach_check={"ok": True, "url": "http://localhost:9222",
                                            "browser": "Chrome/140.0.0.0", "hint": "..."})
    _open_advanced(desktop)
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
    _open_advanced(desktop)
    desktop.page.click("#browser-attach-check")
    desktop.page.wait_for_timeout(250)
    assert "Nothing answered" in desktop.page.inner_text("#browser-attach-status")
    assert desktop.page.eval_on_selector(
        "#browser-attach-status", "e => e.classList.contains('attach-bad')") is True


# --------------------------------------------------------------------- #
# The extension: the path that actually works

def test_it_is_on_by_default(desktop):
    """Installing an unpacked extension into your own browser is already a
    deliberate act. Asking for a second opt-in afterwards is how someone ends
    up with everything set up and nothing working."""
    _open_browser_tab(desktop)
    assert desktop.page.eval_on_selector(
        "#opt-browser-mine", "e => e.getAttribute('aria-checked')") == "true"


def test_switched_off_says_which_browser_is_being_used(desktop):
    _open_browser_tab(desktop, {"browser_own": "off"},
                      browser_extension_status=EXT_DISABLED)
    desktop.page.wait_for_timeout(250)
    assert "its own separate browser" in desktop.page.inner_text("#browser-ext-status")


def test_connected_says_so_plainly(desktop):
    _open_browser_tab(desktop, browser_extension_status=EXT_ON)
    desktop.page.wait_for_timeout(250)
    assert "work in your tabs" in desktop.page.inner_text("#browser-ext-status")


def test_switching_it_back_on_offers_the_instructions(desktop):
    """A switch that appears to do nothing is how this gets abandoned thirty
    seconds in -- the extension is the only thing that makes it work."""
    _open_browser_tab(desktop, {"browser_own": "off"},
                      set_setting=dict(DEFAULT_SETTINGS, browser_own="auto"),
                      browser_extension_status=EXT_WAITING)
    desktop.page.click("#opt-browser-mine")
    desktop.page.wait_for_timeout(350)
    assert desktop.page.eval_on_selector("#browser-ext-sheet", "e => !e.hidden")


def test_the_sheet_shows_the_folder_to_load(desktop):
    """The one thing it exists to hand over. Chrome's "Load unpacked" wants a
    folder, and nobody can guess where this one is."""
    _open_browser_tab(desktop, browser_extension_status=EXT_WAITING)
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(200)
    assert "/home/you/GLMCode/extension" in desktop.page.inner_text("#ext-path")
    assert "chrome://extensions" in desktop.page.inner_text("#browser-ext-sheet")


def test_the_sheet_notices_the_extension_arriving(desktop):
    """It is loaded in ANOTHER window, so nothing in this one gets clicked when
    it works. Without the panel updating itself, the last thing the user sees
    is 'Waiting' and they conclude it failed."""
    _open_browser_tab(desktop, browser_extension_status=EXT_WAITING)
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(200)
    assert "Waiting" in desktop.page.inner_text("#ext-live-status")
    desktop.reply("browser_extension_status", EXT_ON)
    desktop.page.wait_for_timeout(2600)      # the panel polls while it is open
    assert "Connected" in desktop.page.inner_text("#ext-live-status")


def test_the_devtools_route_is_not_the_first_thing_you_see(desktop):
    """It cannot reach a running browser, so offering it at the same level as
    the extension is what made this feature feel broken."""
    _open_browser_tab(desktop)
    assert desktop.page.eval_on_selector(
        "#opt-browser-connect", "e => e.closest('details').open") is False


# --------------------------------------------------------------------- #
# Making the install something a person will actually finish

CHROME = {"name": "Google Chrome", "path": "/usr/bin/google-chrome"}
EDGE = {"name": "Microsoft Edge", "path": "/usr/bin/microsoft-edge"}
TAB = {"url": "https://github.com/you/repo/pulls", "title": "Pull requests"}


def test_it_names_the_browsers_you_actually_have(desktop):
    """"Open chrome://extensions" assumes one browser and assumes it is Chrome.
    Someone who lives in Edge reads that and installs it in the wrong one, then
    wonders why nothing connects. The button only brings that browser forward --
    see test_the_button_does_not_claim_to_navigate."""
    _open_browser_tab(desktop, browser_extension_status=dict(
        EXT_WAITING, browsers=[CHROME, EDGE]))
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    labels = desktop.page.eval_on_selector_all(
        "#ext-browsers button", "els => els.map(e => e.textContent)")
    assert labels == ["Bring Google Chrome to the front",
                      "Bring Microsoft Edge to the front"]


def test_the_address_to_paste_is_the_primary_step(desktop):
    """Reported: the button "is just launching a fresh empty window". It was
    passing chrome://extensions on the command line, which Chrome DROPS -- so
    the browser opened with nothing. Nothing can open another program's
    chrome:// page, so the paste is the step and the panel says why."""
    _open_browser_tab(desktop, browser_extension_status=dict(
        EXT_WAITING, browsers=[CHROME]))
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    assert "chrome://extensions" in desktop.page.inner_text("#ext-store-url")
    assert desktop.page.eval_on_selector(
        "#ext-copy-url", "e => e.classList.contains('btn-primary')")
    # The copy uses a typographic apostrophe, so match on the stable half.
    assert "open another program" in desktop.page.inner_text("#browser-ext-sheet")


def test_clicking_one_brings_that_browser_forward(desktop):
    _open_browser_tab(desktop, browser_extension_status=dict(
        EXT_WAITING, browsers=[CHROME, EDGE]), open_extensions_page={"ok": True})
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    desktop.page.click("#ext-browsers button:nth-child(2)")
    desktop.page.wait_for_timeout(200)
    calls = desktop.calls("open_extensions_page")
    assert calls and calls[0]["args"][0] == "/usr/bin/microsoft-edge"


def test_with_no_browser_found_the_paste_step_still_stands(desktop):
    """The address is the step that always works, so nothing depends on having
    detected a browser."""
    _open_browser_tab(desktop, browser_extension_status=dict(EXT_WAITING, browsers=[]))
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.eval_on_selector("#ext-browsers-none", "e => !e.hidden")
    assert "chrome://extensions" in desktop.page.inner_text("#ext-store-url")


def test_connecting_is_the_whole_of_the_setup(desktop):
    """There is no second step. Installing the extension is what turns this on,
    which is the entire point of the change."""
    _open_browser_tab(desktop, browser_extension_status=dict(
        EXT_ON, browsers=[CHROME]))
    desktop.page.wait_for_timeout(250)
    said = desktop.page.inner_text("#browser-ext-status")
    assert "work in your tabs" in said
    assert "switch" not in said.lower()


def test_the_sheet_says_you_are_done_rather_than_asking_for_one_more_thing(desktop):
    """Connecting IS the setup now. The sheet's last line tells you to just ask
    for something, not to go and find a switch."""
    _open_browser_tab(desktop, browser_extension_status=dict(EXT_ON, browsers=[CHROME]))
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.eval_on_selector("#ext-finish", "e => !e.hidden")
    assert "That is everything" in desktop.page.inner_text("#ext-finish")
    assert desktop.calls("set_setting") == []


def test_the_finish_button_stays_hidden_until_it_can_work(desktop):
    _open_browser_tab(desktop, browser_extension_status=dict(EXT_WAITING, browsers=[CHROME]))
    desktop.page.click("#browser-ext-install")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.eval_on_selector("#ext-finish", "e => e.hidden")


def test_it_names_the_tab_it_would_act_on(desktop):
    """"My own browser" is otherwise a leap of faith taken at the moment the
    agent starts clicking things."""
    _open_browser_tab(desktop,
                      browser_extension_status=dict(EXT_ON, tab=TAB, browsers=[CHROME]))
    desktop.page.wait_for_timeout(250)
    said = desktop.page.inner_text("#browser-ext-tab")
    assert "Pull requests" in said and "github.com" in said


def test_no_tab_line_when_nothing_is_connected(desktop):
    _open_browser_tab(desktop, browser_extension_status=EXT_WAITING)
    desktop.page.wait_for_timeout(250)
    assert desktop.page.eval_on_selector("#browser-ext-tab", "e => e.hidden")
