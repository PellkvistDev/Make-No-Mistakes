"""Sending, and what happens to the keyboard afterwards.

Reported: while typing, the composer sat underneath iOS's own form accessory
bar (prev/next chevrons and Done) and could not be seen; and sending with the
return key left the keyboard up, with the only way out being a Done button on a
bar that has nothing to do with this app.
"""


def test_sending_blurs_the_composer(phone):
    """On iOS the keyboard and its accessory bar stay until something blurs.
    Sending is the end of the thought; the keyboard should go with it."""
    phone.setup()
    p = phone.page
    p.click("#in-prompt")
    assert p.evaluate("() => document.activeElement.id") == "in-prompt"

    phone.reply({"role": "assistant", "content": "ok"})
    p.fill("#in-prompt", "hello")
    p.evaluate("""() => {
      const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      document.getElementById('in-prompt').dispatchEvent(ev);
    }""")
    p.wait_for_timeout(300)
    assert p.evaluate("() => document.activeElement.id") != "in-prompt", \
        "the field kept focus after sending, so the keyboard stays up"
    phone.wait_idle()
    assert phone.errors == []


def test_the_return_key_sends_rather_than_adding_a_newline(phone):
    phone.setup()
    phone.reply({"role": "assistant", "content": "got it"})
    p = phone.page
    p.fill("#in-prompt", "a question")
    p.evaluate("""() => {
      const ev = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      document.getElementById('in-prompt').dispatchEvent(ev);
    }""")
    phone.wait_idle()
    assert p.input_value("#in-prompt") == "", "the composer should empty on send"
    bubbles = p.eval_on_selector_all(".bubble.user", "els => els.map(e => e.textContent)")
    assert any("a question" in b for b in bubbles)
    assert phone.errors == []


def test_shift_return_still_makes_a_newline(phone):
    """Multi-line messages have to stay possible."""
    phone.setup()
    p = phone.page
    p.fill("#in-prompt", "line one")
    p.evaluate("""() => {
      const ev = new KeyboardEvent('keydown',
        { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true });
      document.getElementById('in-prompt').dispatchEvent(ev);
    }""")
    p.wait_for_timeout(200)
    assert p.input_value("#in-prompt") == "line one", "shift+return sent the message"
    assert p.eval_on_selector_all(".bubble.user", "e => e.length") == 0
    assert phone.errors == []


# --------------------------------------------- iOS's own bar above the keys
# iOS draws a form accessory bar (prev/next chevrons and Done) above the
# keyboard for text fields. It belongs to the system and there is no web API to
# remove it, so the question is only whether the app has to leave room for it.
#
# It does not. An earlier version added 44px on iOS on the theory that
# visualViewport shrinks by the keys alone. The device below says that is
# wrong, and the extra 44 parked the composer in mid-air.

def kb_on(phone, app_url, platform, covered=300):
    """Boot the app as if on `platform`, then pretend the keyboard covers
    `covered` px, and report what --kb ended up as.

    The platform is still set BEFORE the app loads. The measurement no longer
    branches on it -- that is the point of two of these tests -- and setting it
    afterwards would silently measure nothing if it ever started to again.
    """
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => %r });" % platform)
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    return phone.page.evaluate("""(covered) => {
      const vv = window.visualViewport;
      Object.defineProperty(vv, 'height',
        { configurable: true, get: () => window.innerHeight - covered });
      vv.dispatchEvent(new Event('resize'));
      return getComputedStyle(document.documentElement).getPropertyValue('--kb').trim();
    }""", covered)


def test_the_dock_lifts_by_exactly_what_is_hidden_and_no_more(phone, app_url):
    """What visualViewport reports as hidden is the whole obstruction, iOS's
    own bar included. Adding anything to it lifts the composer off the keyboard
    and leaves a strip of chat showing in the gap."""
    assert kb_on(phone, app_url, "iPhone") == "300px", \
        "the dock was lifted further than the keyboard actually covers"
    assert phone.errors == []


def test_the_lift_does_not_depend_on_which_device_this_is(phone, app_url):
    """One subtraction, no per-platform correction to get wrong."""
    assert kb_on(phone, app_url, "Linux x86_64") == "300px"
    assert phone.errors == []


def test_a_collapsing_address_bar_is_still_not_mistaken_for_a_keyboard(phone, app_url):
    assert kb_on(phone, app_url, "iPhone", covered=60) == "0px"
    assert phone.errors == []


# The numbers an iPhone 15 Pro actually reported, read out of the app's own
# diagnostics with the keyboard up (build ff4cf29):
#
#   at rest        while typing
#   inner h   852  inner h   449     <- 852 is the real screen height
#   html h    793  html h    793
#   visual h  852  visual h  449
#   --safe-t   59  --safe-t   59
#
# 852 - 59 - 449 = 344 hidden, and 344 is the keys AND the accessory bar: the
# composer has to stop at 344, not 388. This is the case no desktop browser can
# produce on its own, so it is pinned here with the device's own figures.
IPHONE_15_PRO = {"screen": 852, "layout": 793, "visible": 449, "safe_t": 59}


def test_the_iphone_numbers_that_were_actually_measured(phone, app_url):
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => 'iPhone' });")
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    got = phone.page.evaluate("""(d) => {
      // Reproduce the device: the layout viewport stays put while the visual
      // one and innerHeight both shrink to the visible strip.
      Object.defineProperty(document.documentElement, 'clientHeight',
        { configurable: true, get: () => d.layout });
      Object.defineProperty(window, 'innerHeight',
        { configurable: true, get: () => d.visible });
      const vv = window.visualViewport;
      Object.defineProperty(vv, 'height', { configurable: true, get: () => d.visible });
      Object.defineProperty(vv, 'offsetTop', { configurable: true, get: () => 0 });
      vv.dispatchEvent(new Event('resize'));
      return getComputedStyle(document.documentElement).getPropertyValue('--kb').trim();
    }""", IPHONE_15_PRO)

    hidden = IPHONE_15_PRO["screen"] - IPHONE_15_PRO["safe_t"] - IPHONE_15_PRO["visible"]
    assert got == f"{hidden}px", (
        f"on the phone this was measured from, the keyboard and iOS's bar cover "
        f"{hidden}px; --kb came out {got}, which puts the composer "
        f"{int(got[:-2]) - hidden}px off the keyboard")
    assert phone.errors == []


def kb_when_inner_height_also_shrinks(phone, app_url, platform, covered=300):
    """The installed-PWA case: window.innerHeight tracks the VISUAL viewport,
    so it shrinks with the keyboard too and innerHeight - vv.height is ~0.

    This is the one that mattered. The dock never lifted; iOS scrolling the
    document to reveal the focused field was putting the composer on screen by
    accident, and removing that shove is what exposed it.
    """
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => %r });" % platform)
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    return phone.page.evaluate("""(covered) => {
      const vv = window.visualViewport;
      const layout = document.documentElement.clientHeight;
      Object.defineProperty(vv, 'height',
        { configurable: true, get: () => layout - covered });
      // The engine under test: innerHeight follows the visible area, not the
      // layout viewport.
      Object.defineProperty(window, 'innerHeight',
        { configurable: true, get: () => layout - covered });
      vv.dispatchEvent(new Event('resize'));
      return getComputedStyle(document.documentElement).getPropertyValue('--kb').trim();
    }""", covered)


def test_the_dock_still_lifts_when_innerheight_shrinks_with_the_keyboard(phone, app_url):
    got = kb_when_inner_height_also_shrinks(phone, app_url, "iPhone")
    assert got == "300px", (
        "the dock did not lift: the measurement used a reference that shrinks "
        f"with the keyboard, so it saw nothing covered (--kb = {got})")
    assert phone.errors == []


def test_the_same_holds_on_a_device_reporting_another_platform(phone, app_url):
    got = kb_when_inner_height_also_shrinks(phone, app_url, "Linux x86_64")
    assert got == "300px", f"--kb = {got}"
    assert phone.errors == []


def test_a_scrolled_visual_viewport_is_subtracted_too(phone, app_url):
    """visualViewport.offsetTop is how far the visible area has been pushed
    down the layout viewport -- iOS does that when zoomed, or mid-scroll to a
    focused field. Those pixels are off the bottom as surely as the keyboard's
    are, so they belong in the subtraction. The measured phone reported 0 for
    it, which means this branch is the one the device could not confirm.
    """
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => 'iPhone' });")
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    got = phone.page.evaluate("""() => {
      const vv = window.visualViewport;
      const layout = document.documentElement.clientHeight;
      Object.defineProperty(vv, 'height', { configurable: true, get: () => layout - 300 });
      Object.defineProperty(vv, 'offsetTop', { configurable: true, get: () => 40 });
      vv.dispatchEvent(new Event('resize'));
      return getComputedStyle(document.documentElement).getPropertyValue('--kb').trim();
    }""")
    assert got == "260px", (
        "the 40px the visible area was pushed down was not taken off the lift, "
        f"so the composer rides 40px too high (--kb = {got})")
    assert phone.errors == []


# ------------------------------------------------------------ diagnostics --
# The keyboard took three attempts because every number that would have
# answered it lives on the device and nowhere else. They are in the app now.

def test_settings_reports_the_numbers_that_describe_this_screen(phone):
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    text = phone.page.inner_text("#set-diag")
    for key in ("build", "standalone", "fixed box h", "html h", "inner h", "visual h",
                "--kb", "--safe-t", "--safe-b", "dock bottom"):
        assert key in text, f"{key!r} missing from the diagnostics:\n{text}"
    assert phone.errors == []


def test_the_readout_is_live_while_the_keyboard_is_up(phone):
    """A snapshot taken before the keyboard opened describes the wrong state,
    which is exactly the state nobody can observe from a desktop."""
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    phone.page.evaluate("() => document.documentElement.style.setProperty('--kb', '311px')")
    phone.page.wait_for_function(
        "() => document.getElementById('set-diag').innerText.includes('311px')",
        timeout=5000)
    assert phone.errors == []


def test_the_keyboard_up_numbers_survive_the_keyboard_going_down(phone):
    """The live readout alone answers nothing. Opening Settings takes focus off
    the composer, so the keyboard drops and every value that describes it is
    already gone by the time the panel is on screen. The numbers have to be
    recorded while the keyboard is up and still be there afterwards."""
    phone.setup()
    phone.page.evaluate("""() => {
      const vv = window.visualViewport;
      const layout = document.documentElement.clientHeight;
      Object.defineProperty(vv, 'height',
        { configurable: true, get: () => layout - 300 });
      vv.dispatchEvent(new Event('resize'));
    }""")
    # ...and now the keyboard goes away, exactly as it does when you reach for
    # Settings.
    phone.page.evaluate("""() => {
      delete window.visualViewport.height;
      window.visualViewport.dispatchEvent(new Event('resize'));
    }""")
    assert phone.page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--kb').trim()") == "0px", "the keyboard did not go down"

    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    text = phone.page.inner_text("#set-diag")
    assert "while typing" in text, f"no recording section:\n{text}"
    recorded = text.split("while typing", 1)[1]
    assert "300px" in recorded, (
        "the keyboard-up numbers were not kept, so the panel only ever shows "
        f"the state with the keyboard down:\n{text}")


def test_the_recording_says_which_field_raised_the_keyboard(phone):
    """A reading came back with the dock's rect at 0/0 -- a zero rect means the
    element was not rendered at that instant -- and nothing in it said whether
    the keyboard had been raised by the composer or by a field in a sheet on top
    of it. The numbers were unattributable, so they answered nothing."""
    phone.setup()
    phone.page.click("#in-prompt")
    phone.page.evaluate("""() => {
      const vv = window.visualViewport;
      const layout = document.documentElement.clientHeight;
      Object.defineProperty(vv, 'height', { configurable: true, get: () => layout - 300 });
      vv.dispatchEvent(new Event('resize'));
    }""")
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    recorded = phone.page.inner_text("#set-diag").split("while typing", 1)[-1]
    assert "in-prompt" in recorded and "(the composer)" in recorded, (
        "the recording does not say which field it came from, so its numbers "
        f"cannot be attributed to anything:\n{recorded}")
    assert phone.errors == []


def test_the_build_stamp_is_shown_so_a_stale_cache_is_visible(phone):
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    line = [l for l in phone.page.inner_text("#set-diag").splitlines()
            if l.startswith("build:")]
    assert line and line[0].split(":", 1)[1].strip(), "no build identifier shown"
    assert phone.errors == []

