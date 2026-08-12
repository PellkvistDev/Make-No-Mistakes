"""Setting up a phone from the desktop.

The sheet used to open straight onto the pairing QR -- a dense code carrying
the sealed keys -- with one button reading "Just the link, no keys" and no way
back from it. Three things were wrong with that, and they compound:

  * the two codes do different jobs at different moments, and the sheet
    presented them as one thing with an escape hatch
  * the escape hatch was one-way, so getting the other code back meant closing
    and reopening the sheet
  * opening the sheet MINTED a pairing code and started its five-minute clock
    -- while you were still installing the app. The code you finally scan has
    often already expired, which reads as pairing being broken rather than as
    a timer that started too early.

The real sequence is: install the app, open it, then scan the keys into it --
because on iOS an installed app has its own storage, so keys scanned with the
camera land in Safari and never reach it.
"""

PAIR = {"code": "AB12CD", "svg": "<svg id='pair-svg'></svg>", "ttl": 300,
        "includes": {"model_key": True, "github": True, "sync": False,
                     "providers": 2}}
LINK = {"url": "https://example.github.io/app/", "svg": "<svg id='link-svg'></svg>"}


def _open(desktop, **replies):
    desktop.boot(get_pair_phone=PAIR, get_phone_app=LINK, **replies)
    desktop.page.evaluate("() => document.getElementById('gh-get-app').click()")
    desktop.page.wait_for_timeout(300)
    return desktop


def _step(desktop):
    return desktop.page.evaluate(
        """() => [...document.querySelectorAll('#phoneapp-step button')]
             .filter(b => b.classList.contains('on')).map(b => b.dataset.v)[0]""")


def _calls(desktop, name):
    return desktop.page.evaluate(
        f"() => window.__calls.filter(c => c.name === '{name}').length")


def test_it_opens_on_installing_not_on_the_keys(desktop):
    """The first thing you do, and the code that does not expire."""
    _open(desktop)
    assert _step(desktop) == "install"
    assert desktop.page.eval_on_selector("#phoneapp-qr", "e => e.innerHTML") \
        == "<svg id=\"link-svg\"></svg>"
    assert desktop.errors == []


def test_no_pairing_code_is_minted_until_it_is_asked_for(desktop):
    """Its clock starts the moment it exists. Minting one on open spent the
    whole five minutes on the part of the job that does not need it."""
    _open(desktop)
    assert _calls(desktop, "get_pair_phone") == 0


def test_the_two_steps_go_both_ways(desktop):
    """The old escape hatch was one-way: once you asked for the plain link
    there was no route back to the pairing code short of reopening."""
    _open(desktop)
    for want, svg in (("keys", "pair-svg"), ("install", "link-svg"),
                      ("keys", "pair-svg")):
        desktop.page.evaluate(
            f"""() => document.querySelector(
                  '#phoneapp-step button[data-v="{want}"]').click()""")
        desktop.page.wait_for_timeout(250)
        assert _step(desktop) == want
        assert svg in desktop.page.eval_on_selector("#phoneapp-qr", "e => e.innerHTML")


def test_the_keys_step_says_where_to_scan_it(desktop):
    """Scanning this with the camera puts the keys in Safari, and an installed
    iOS app has its own storage -- so they never reach the app just installed,
    which looks like pairing silently doing nothing."""
    _open(desktop)
    desktop.page.evaluate(
        """() => document.querySelector(
              '#phoneapp-step button[data-v="keys"]').click()""")
    desktop.page.wait_for_timeout(300)
    hint = desktop.page.text_content("#phoneapp-hint")
    assert "in the app" in hint.lower()
    assert "Scan a code from your computer" in hint


def test_the_install_step_says_to_open_the_app_before_step_two(desktop):
    _open(desktop)
    assert "before doing step 2" in desktop.page.text_content("#phoneapp-hint")


def test_the_keys_step_lists_what_it_will_send(desktop):
    _open(desktop)
    desktop.page.evaluate(
        """() => document.querySelector(
              '#phoneapp-step button[data-v="keys"]').click()""")
    desktop.page.wait_for_timeout(300)
    said = desktop.page.text_content("#phoneapp-includes")
    for part in ("model key", "GitHub token", "2 APIs"):
        assert part in said
    # The one property that makes a photographed QR useless on its own.
    assert "isn't in the image" in said


def test_the_link_is_offered_for_copying_and_the_sealed_one_is_not(desktop):
    """There is no safe way to paste a link with the keys in it around, so the
    UI does not offer one."""
    _open(desktop)
    assert desktop.page.eval_on_selector("#phoneapp-copy", "e => e.hidden") is False
    desktop.page.evaluate(
        """() => document.querySelector(
              '#phoneapp-step button[data-v="keys"]').click()""")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.eval_on_selector("#phoneapp-copy", "e => e.hidden") is True


def test_a_pairing_that_cannot_be_built_leaves_step_one_reachable(desktop):
    """No URL, no keys, or no crypto. The sheet used to swap itself to the
    plain link; now it says why and leaves the choice where it was."""
    desktop.boot(get_pair_phone={"error": "Set the phone-app URL first."},
                 get_phone_app=LINK)
    desktop.page.evaluate("() => document.getElementById('gh-get-app').click()")
    desktop.page.wait_for_timeout(300)
    desktop.page.evaluate(
        """() => document.querySelector(
              '#phoneapp-step button[data-v="keys"]').click()""")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.eval_on_selector("#phoneapp-error", "e => e.hidden") is False
    assert "URL" in desktop.page.text_content("#phoneapp-error")
    assert "type your keys in by hand" in desktop.page.text_content("#phoneapp-hint")
    # And going back still works.
    desktop.page.evaluate(
        """() => document.querySelector(
              '#phoneapp-step button[data-v="install"]').click()""")
    desktop.page.wait_for_timeout(250)
    assert _step(desktop) == "install"
