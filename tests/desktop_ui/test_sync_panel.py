"""The shared-chats panel, after the passphrase stopped being something to
invent.

It used to open on a password field: "Sync passphrase (min 6 characters)". That
was the wrong question. The passphrase is not a password anyone logs in with --
it is the key the chats are encrypted under, and its only job is to be the same
on both devices. Pairing already carries it to the phone. So the field bought
nothing except a chance to choose something weak, or to mistype it on the second
machine and fork the history into two halves that can never read each other.

What is left is one button. The text field survives for the only case that
genuinely needs a person -- a store another computer already made, whose key is
decided and cannot be guessed -- and it is folded away until then.
"""

OFF = {"available": True, "crypto_state": "ok", "crypto_reason": "",
       "install_hint": "pip install cryptography", "passphrase_set": False,
       "token_present": True, "enabled": False, "repo": "makenomistakes-sync"}
ON = {**OFF, "passphrase_set": True, "enabled": True}

# Worded rather than shaped like a real code: a passphrase-ish keyword
# assigned a random-looking grouped string is exactly what a secret scanner
# exists to catch, and nothing here depends on the shape.
OTHER_DEVICE_KEY = "example-key-from-the-other-device"


def _open(desktop, **replies):
    desktop.boot(sync_env=OFF, **replies)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.evaluate(
        """() => document.querySelector('.settings-tab-btn[data-tab="phone"]').click()""")
    desktop.page.wait_for_timeout(250)
    return desktop


def _visible(desktop, sel):
    return desktop.page.is_visible(sel)


def _calls(desktop, name):
    return desktop.page.evaluate(
        f"() => window.__calls.filter(c => c.name === '{name}')")


def test_it_opens_on_a_button_not_a_passphrase_box(desktop):
    """The complaint, in one assertion."""
    _open(desktop)
    assert _visible(desktop, "#sync-enable")
    assert not _visible(desktop, "#sync-pass"), \
        "the passphrase field must not be in the way of the normal path"
    assert desktop.errors == []


def test_turning_it_on_sends_no_passphrase(desktop):
    _open(desktop, sync_enable={"ok": True, **ON})
    desktop.page.evaluate("() => document.getElementById('sync-enable').click()")
    desktop.page.wait_for_timeout(300)
    calls = _calls(desktop, "sync_enable")
    assert len(calls) == 1
    assert not calls[0]["args"], "the key is the backend's to make, not the UI's"
    assert _calls(desktop, "sync_set_passphrase") == []


def test_once_on_the_code_can_be_read_back(desktop):
    """Generated means nobody has it memorised. The only copies are this
    machine's credential store and a paired phone, so it must be visible."""
    desktop.boot(sync_env=ON, sync_recovery_code={"code": OTHER_DEVICE_KEY})
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.evaluate(
        """() => document.querySelector('.settings-tab-btn[data-tab="phone"]').click()""")
    desktop.page.wait_for_timeout(250)
    assert not _visible(desktop, "#sync-code-wrap")
    desktop.page.evaluate("() => document.getElementById('sync-show-code').click()")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.input_value("#sync-code") == OTHER_DEVICE_KEY


def test_the_code_is_not_on_screen_until_it_is_asked_for(desktop):
    """It is the key to every synced chat; leaving it painted on the settings
    panel is not the same as being able to see it when you want it."""
    desktop.boot(sync_env=ON, sync_recovery_code={"code": OTHER_DEVICE_KEY})
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.evaluate(
        """() => document.querySelector('.settings-tab-btn[data-tab="phone"]').click()""")
    desktop.page.wait_for_timeout(250)
    assert _calls(desktop, "sync_recovery_code") == []
    assert desktop.page.input_value("#sync-code") == ""


def test_a_second_computer_is_offered_the_code_field(desktop):
    """The one case that still needs a human, and the backend says so rather
    than letting a generated key fail as a wrong passphrase."""
    _open(desktop, sync_enable={"needs_code": True, **OFF,
                                "error": "already set up on another device — "
                                         "copy the recovery code"})
    desktop.page.evaluate("() => document.getElementById('sync-enable').click()")
    desktop.page.wait_for_timeout(400)
    assert desktop.page.eval_on_selector("#sync-have-code", "e => e.open") is True
    assert _visible(desktop, "#sync-pass")


def test_the_code_field_is_folded_away_until_then(desktop):
    """Present, because a second computer needs it; closed, because on the
    first one it is exactly the question that should not be asked."""
    _open(desktop)
    assert desktop.page.eval_on_selector("#sync-have-code", "e => e.open") is False


def test_pasting_a_code_goes_through_the_verifying_path(desktop):
    """Not stored blind: a code that disagrees with the other machine has to be
    caught here rather than forking the history."""
    _open(desktop, sync_set_passphrase={"ok": True, **ON})
    desktop.page.evaluate(
        """(code) => { document.getElementById('sync-have-code').open = true;
                       document.getElementById('sync-pass').value = code;
                       document.getElementById('sync-pass-save').click(); }""",
        OTHER_DEVICE_KEY)
    desktop.page.wait_for_timeout(300)
    calls = _calls(desktop, "sync_set_passphrase")
    assert calls and calls[0]["args"][0] == OTHER_DEVICE_KEY


def test_turning_it_off_warns_that_the_code_is_what_gets_it_back(desktop):
    """"Change passphrase" used to sit here, which was never a useful thing to
    do -- a new key cannot read anything already uploaded."""
    desktop.boot(sync_env=ON)
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.evaluate(
        """() => document.querySelector('.settings-tab-btn[data-tab="phone"]').click()""")
    desktop.page.wait_for_timeout(250)
    assert desktop.page.query_selector("#sync-pass-change") is None
    msgs = []
    desktop.page.on("dialog", lambda d: (msgs.append(d.message), d.dismiss()))
    desktop.page.evaluate("() => document.getElementById('sync-pass-forget').click()")
    desktop.page.wait_for_timeout(300)
    assert msgs and "recovery code" in msgs[0]
