"""Talking to the phone.

The phone is the device where typing is worst and talking is most obvious, and
it was the only one with no voice at all. The Live API fits its hardest
constraint exactly: the session is a WebSocket opened by the page, so there is
still no backend to run.

No socket is opened here. What is checked is what is wrong silently -- whether
the button appears at all, what a session would be built out of, and the CSP,
which blocks a WebSocket to a host it already allows over https and reports it
as a bare console error with no request in the network tab.
"""

import re
from pathlib import Path

MOBILE = Path(__file__).resolve().parents[2] / "mobile"

LIVE_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _with(phone, rows):
    """Set the APIs, then go through setup into the chat screen.

    Written before setup rather than after a relaunch, because a relaunch
    leaves the app LOCKED -- and on the unlock screen every one of these
    assertions passes for the wrong reason, since the composer is hidden too.
    """
    phone.page.evaluate(
        "(rows) => localStorage.setItem('mnm.providers', JSON.stringify(rows))",
        rows)
    # No sync: this has nothing to do with the sync store, and standing up a
    # passphrase costs a slow modal per test.
    phone.setup(sync_pass=None)
    phone.page.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    return phone


# ---- the wall ----------------------------------------------------------- #

def test_the_socket_host_is_allowed_over_wss_specifically():
    """connect-src matches on SCHEME. A wss: connection to a host allowed only
    over https: is blocked, and the failure is a console line with nothing in
    the network tab to explain it."""
    csp = _csp()
    connect = re.search(r"connect-src([^;]*);", csp, re.S).group(1)
    assert "wss://generativelanguage.googleapis.com" in connect
    # And the wall is still a wall.
    assert "default-src 'none'" in csp
    assert "*" not in connect


def _csp():
    # The policy itself, not the file: the commentary around it names the
    # things it forbids, so searching the whole file finds "unsafe-eval" in
    # the sentence promising there is no unsafe-eval.
    return re.search(r'Content-Security-Policy" content="(.*?)"',
                     (MOBILE / "index.html").read_text(encoding="utf-8"),
                     re.S).group(1)


def test_the_page_never_widens_the_wall_to_get_a_socket():
    """A tempting fix for the above is to loosen connect-src to a wildcard.
    The CSP is this app's outer wall precisely because the keys live here."""
    csp = _csp()
    assert "unsafe-eval" not in csp
    assert "connect-src *" not in csp


# ---- whether it is offered ---------------------------------------------- #

def test_no_button_without_an_api_that_can_do_it(phone):
    """Most APIs do not implement this protocol at all, and the phone can hold
    several since pairing started syncing them."""
    _with(phone, [{"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4",
                   "key": "zk", "models": ["glm-4.7-flash"]}])
    assert phone.page.eval_on_selector("#btn-voice", "e => e.hidden") is True


def test_the_button_appears_once_a_live_capable_key_is_there(phone):
    _with(phone, [
        {"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "zk",
         "models": ["glm-4.7-flash"]},
        {"name": "Google AI Studio", "baseUrl": LIVE_BASE, "key": "gk",
         "models": ["gemini-3.6-flash"]},
    ])
    # And the chat screen really is up, so "hidden" means hidden rather than
    # "the whole composer is off-screen behind the unlock prompt".
    assert phone.page.eval_on_selector("#btn-send", "e => e.offsetParent !== null")
    assert phone.page.eval_on_selector("#btn-voice", "e => e.hidden") is False


def test_a_live_capable_provider_with_no_key_is_not_enough(phone):
    """Paired providers arrive keyless when the desktop had nothing to send."""
    _with(phone, [{"name": "Google AI Studio", "baseUrl": LIVE_BASE,
                   "key": "", "models": ["gemini-3.6-flash"]}])
    assert phone.page.eval_on_selector("#btn-voice", "e => e.hidden") is True


# ---- what a session would be -------------------------------------------- #

def test_the_session_is_given_the_tools_that_read_and_the_one_that_hands_off(phone):
    """Reading, so it can answer from the repo rather than from memory, and
    needs_desktop, because a phone has no shell. Writing is deliberately absent:
    a commit you cannot see the diff of, from a sentence that might have been
    misheard, is not a hands-free action."""
    phone.setup()
    names = phone.page.evaluate(
        """() => AgentCore.liveFunctionDeclarations(
             [...AgentCore.TOOL_SCHEMAS, AgentCore.NEEDS_DESKTOP_SCHEMA]
               .filter(t => ["list_dir", "glob", "read_file", "grep",
                             "search_code", "needs_desktop"]
                 .includes(t.function.name))).map(d => d.name)""")
    assert set(names) == {"list_dir", "glob", "read_file", "grep",
                          "search_code", "needs_desktop"}
    assert "write_file" not in names and "edit_file" not in names


def test_the_spoken_prompt_is_not_the_written_one(phone):
    """The coding prompt asks for paths, code blocks and diffs, none of which
    survive being read out loud."""
    phone.setup()
    spoken, written = phone.page.evaluate(
        "() => [AgentCore.LIVE_VOICE_PROMPT, AgentCore.SYSTEM_PROMPT]")
    assert spoken != written
    assert "out loud" in spoken
    assert "needs_desktop" in spoken


def test_the_sheet_stays_out_of_the_way_until_it_is_wanted(phone):
    phone.setup()
    assert phone.page.eval_on_selector("#voice-backdrop", "e => e.hidden") is True
