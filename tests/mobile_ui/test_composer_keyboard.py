"""Sending, and what happens to the keyboard afterwards.

Reported: while typing, the composer sat underneath iOS's own form accessory
bar (prev/next chevrons and Done) and could not be seen; and sending with the
return key left the keyboard up, with the only way out being a Done button on a
bar that has nothing to do with this app.
"""

import re


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
# keyboard for text fields. It belongs to the system, there is no web API to
# remove it, and -- the part that got this wrong twice -- no web API reports it
# either. It is a native view painted OVER the web view: it does not shrink
# visualViewport, so nothing measured from the page contains its height.
#
# So --kb has two parts, and they are tested separately.
#
#   1. What visualViewport says is hidden. Pure arithmetic, tested below with
#      the allowance pinned to 0 so these keep measuring only the subtraction.
#   2. The allowance for the bar. Not measurable here at all; it is a stored
#      number the device settles. Its own tests are further down.
#
# The earlier reading -- "852 - 449 - 59 = 344, which is the keys AND the bar
# together" -- does not follow, and the check used to confirm it (dock bottom ==
# visual h) was measuring the wrong invariant: flush with the bottom of the
# visible area IS underneath a bar painted across it.

def pin_allowance(phone, px):
    """Fix the accessory-bar allowance before the app loads.

    Every subtraction test pins it, so that changing the shipped default can
    never quietly move their expectations.
    """
    phone.page.add_init_script(
        "try { localStorage.setItem('mnm.kb.bar', %r); } catch (e) {}" % str(px))


def kb_on(phone, app_url, platform, covered=300, allowance=0):
    """Boot the app as if on `platform`, then pretend the keyboard covers
    `covered` px, and report what --kb ended up as.

    The platform is still set BEFORE the app loads. The measurement no longer
    branches on it -- that is the point of two of these tests -- and setting it
    afterwards would silently measure nothing if it ever started to again.
    """
    pin_allowance(phone, allowance)
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


def test_the_dock_lifts_by_what_visualviewport_says_is_hidden(phone, app_url):
    """The subtraction on its own, with no allowance in the way."""
    assert kb_on(phone, app_url, "iPhone") == "300px", \
        "the dock was lifted by something other than what the keyboard covers"
    assert phone.errors == []


def test_the_lift_does_not_depend_on_which_device_this_is(phone, app_url):
    """One subtraction, no per-platform correction to get wrong."""
    assert kb_on(phone, app_url, "Linux x86_64") == "300px"
    assert phone.errors == []


def test_the_accessory_allowance_is_added_on_top_of_what_is_hidden(phone, app_url):
    """The bar is painted over the visible area, so its height is never part of
    what visualViewport reports and has to be added. With it missing the
    composer sits exactly one bar too low -- behind the row with the checkmark,
    which is the reported symptom."""
    assert kb_on(phone, app_url, "iPhone", allowance=44) == "344px", \
        "the allowance was not added, so the composer stays under iOS's bar"
    assert phone.errors == []


def test_a_collapsing_address_bar_is_still_not_mistaken_for_a_keyboard(phone, app_url):
    assert kb_on(phone, app_url, "iPhone", covered=60) == "0px"
    assert phone.errors == []


def test_no_allowance_is_added_while_the_keyboard_is_down(phone, app_url):
    """There is no bar without a keyboard. Adding the allowance to a --kb of 0
    would hold the composer permanently off the bottom of the screen."""
    assert kb_on(phone, app_url, "iPhone", covered=60, allowance=44) == "0px", \
        "the composer was lifted by the allowance with no keyboard on screen"
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
# 852 - 59 - 449 = 344 hidden. That 344 is what the KEYBOARD covers; the
# accessory bar is drawn over the 449 that is still visible and is not in this
# number at all -- reading it as "the keys and the bar together" is what left
# the composer underneath the bar. The allowance is pinned to 0 here so this
# case keeps testing the subtraction against the device's own figures.
IPHONE_15_PRO = {"screen": 852, "layout": 793, "visible": 449, "safe_t": 59}


def test_the_iphone_numbers_that_were_actually_measured(phone, app_url):
    pin_allowance(phone, 0)
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
        f"on the phone this was measured from, the keyboard covers {hidden}px; "
        f"--kb came out {got}, which puts the composer "
        f"{int(got[:-2]) - hidden}px off the keyboard")
    assert phone.errors == []


def kb_when_inner_height_also_shrinks(phone, app_url, platform, covered=300):
    """The installed-PWA case: window.innerHeight tracks the VISUAL viewport,
    so it shrinks with the keyboard too and innerHeight - vv.height is ~0.

    This is the one that mattered. The dock never lifted; iOS scrolling the
    document to reveal the focused field was putting the composer on screen by
    accident, and removing that shove is what exposed it.
    """
    pin_allowance(phone, 0)
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
    pin_allowance(phone, 0)
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


# -------------------------------------------- settling the allowance on-device
# The bar's height cannot be measured from the page, so the number is stored and
# adjustable, and the control to adjust it appears on the composer while the
# keyboard is up -- the only moment the bar is drawn and the gap can be judged.
# These cover the ways a stored number can go wrong and take the dock with it.

def boot_with_allowance(phone, app_url, raw):
    """Load with `raw` already written to the allowance key, keyboard down."""
    phone.page.add_init_script(
        "try { localStorage.setItem('mnm.kb.bar', %r); } catch (e) {}" % raw)
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => 'iPhone' });")
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)


def kb_up(phone, covered=300):
    return phone.page.evaluate("""(covered) => {
      const vv = window.visualViewport;
      Object.defineProperty(vv, 'height',
        { configurable: true, get: () => window.innerHeight - covered });
      vv.dispatchEvent(new Event('resize'));
      return getComputedStyle(document.documentElement).getPropertyValue('--kb').trim();
    }""", covered)


def test_an_unreadable_stored_allowance_falls_back_instead_of_poisoning_kb(phone, app_url):
    """parseInt('') is NaN, and 300 + NaN is NaN. '--kb: NaNpx' is invalid, so
    the declaration is dropped and the dock falls to the bottom of the screen --
    the whole bug this is meant to fix, arrived at from a corrupt value."""
    boot_with_allowance(phone, app_url, "not-a-number")
    got = kb_up(phone)
    assert got == "344px", f"a junk stored value reached --kb (= {got})"
    assert phone.errors == []


def test_a_negative_stored_allowance_cannot_pull_the_composer_down(phone, app_url):
    boot_with_allowance(phone, app_url, "-200")
    assert kb_up(phone) == "300px", "a negative allowance was applied"
    assert phone.errors == []


def test_an_absurd_stored_allowance_is_clamped(phone, app_url):
    """Held down on the + button, or a value from a future build."""
    boot_with_allowance(phone, app_url, "9999")
    assert kb_up(phone) == "420px", "the allowance was not clamped"
    assert phone.errors == []


def test_adjusting_the_allowance_moves_the_composer_immediately(phone, app_url):
    """The keyboard is already up while this is being adjusted, and an
    already-open keyboard never resizes -- so nothing fires on its own. Without
    an explicit re-measure the +/- buttons would appear to do nothing until the
    keyboard was next dismissed and raised."""
    boot_with_allowance(phone, app_url, "44")
    assert kb_up(phone) == "344px"
    phone.page.evaluate(
        "() => document.getElementById('kb-cal-up').dispatchEvent("
        "new PointerEvent('pointerdown', { bubbles: true, cancelable: true }))")
    phone.page.wait_for_timeout(80)
    got = phone.page.evaluate(
        "() => getComputedStyle(document.documentElement)"
        ".getPropertyValue('--kb').trim()")
    assert got == "346px", (
        f"the composer did not move when the allowance changed (--kb = {got})")
    assert phone.errors == []


def test_the_adjustment_survives_a_reload(phone, app_url):
    """A value that has to be dialled in again every launch is not a fix.

    Deliberately boots on the shipped default rather than a pinned value: an
    init script would rewrite the key on the reload and the test would pass
    without anything having been remembered.
    """
    phone.page.add_init_script(
        "Object.defineProperty(navigator, 'platform', { get: () => 'iPhone' });")
    phone.open_at(app_url)
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    before = kb_up(phone)
    phone.page.evaluate(
        "() => document.getElementById('kb-cal-up').dispatchEvent("
        "new PointerEvent('pointerdown', { bubbles: true, cancelable: true }))")
    phone.page.wait_for_timeout(80)
    phone.page.reload(wait_until="domcontentloaded")
    phone.page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    after = kb_up(phone)
    assert int(after[:-2]) == int(before[:-2]) + 2, (
        f"the adjustment was not remembered across a launch "
        f"(before {before}, nudged +2, after reload {after})")
    assert phone.errors == []


def test_the_nudge_buttons_do_not_take_focus_off_the_composer(phone):
    """Focus moving off the textarea puts the keyboard away, and the bar with
    it. The control would dismiss the very thing it is measuring against."""
    phone.setup()
    phone.page.click("#in-prompt")
    assert phone.page.evaluate("() => document.activeElement.id") == "in-prompt"
    cancelled = phone.page.evaluate("""() => {
      const ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true });
      document.getElementById('kb-cal-up').dispatchEvent(ev);
      return ev.defaultPrevented;
    }""")
    assert cancelled, \
        "pointerdown was not prevented, so the tap moves focus and drops the keyboard"
    assert phone.page.evaluate("() => document.activeElement.id") == "in-prompt", \
        "the nudge stole focus, which on the device puts the keyboard away"
    assert phone.errors == []


def test_the_calibration_strip_is_out_of_the_way_unless_asked_for(phone):
    """It is a repair tool, not part of the composer."""
    phone.setup()
    assert phone.page.is_hidden("#kb-calibrate"), \
        "the calibration control is on the composer without being asked for"
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    phone.page.check("#set-kb-cal")
    phone.page.click("#btn-settings-done")
    assert phone.page.is_visible("#kb-calibrate"), \
        "switching calibration on in Settings did not show the control"
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
    # Against the allowance the recording itself reports, rather than a literal:
    # this test is about the numbers surviving, and should not fail merely
    # because the shipped default gap changed.
    m = re.search(r"accessory allowance: (\d+)px", recorded)
    assert m, f"no allowance row in the recording:\n{text}"
    expected = 300 + int(m.group(1))
    assert f"--kb: {expected}px" in recorded, (
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


def test_the_recording_says_what_moved_when_a_field_was_focused(phone):
    """The flash on focus has been blamed on two different mechanisms and both
    were wrong, because nothing measured which thing actually moved. There are
    three candidates and they need different fixes: the document scrolling, iOS
    shifting the visual viewport instead, and <html>'s own box changing size.
    All three are now sampled across the keyboard animation.

    NOT COVERED: that the scrollY row reports a real scroll. Deleting the line
    that samples it leaves this test green, because the document cannot be
    scrolled here -- pinDocument puts it back and there is no slack to move --
    so the row reads 0px whether it is sampled or not. Driving a scroll inside
    the sampling window races the scroll listener. Left as a known gap rather
    than a flaky assertion; the other two rows are what this round is for.
    """
    phone.setup()
    # Give each candidate a distinct, non-zero displacement to find, or the
    # rows render as a row of zeroes whether they are sampled or not -- which
    # is what an earlier version of this test asserted, and a mutation that
    # stopped sampling the visual viewport entirely sailed through it.
    phone.page.evaluate("""() => {
      Object.defineProperty(window.visualViewport, 'offsetTop',
        { configurable: true, get: () => 40 });
    }""")
    phone.page.click("#in-prompt")
    # The height has to change DURING the sampling window: the sampler records
    # a delta from where it started, so anything done before focus is simply
    # part of the baseline. (--safe-t is 0 here, so kb-open moves nothing by
    # itself and this stands in for the movement a real inset would produce.)
    phone.page.evaluate("""() => {
      document.documentElement.style.minHeight =
        (document.documentElement.getBoundingClientRect().height + 25) + 'px';
    }""")
    phone.page.wait_for_timeout(900)          # the sampler runs for 700ms
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    text = phone.page.inner_text("#set-diag")
    assert "on focus" in text, f"no focus recording at all:\n{text}"
    recorded = text.split("on focus", 1)[1]
    assert "nothing recorded yet" not in recorded, \
        f"focusing the composer recorded nothing:\n{recorded}"
    for key in ("scrollY moved", "visual top moved", "html height moved"):
        assert key in recorded, (
            f"{key!r} missing, so that candidate cannot be ruled in or out:\n{recorded}")
    assert "visual top moved: 40px" in recorded, (
        "the visual viewport moved 40px and the recording did not see it -- that "
        f"is the one candidate no scroll handler can correct:\n{recorded}")
    moved = [l for l in recorded.splitlines() if "html height moved" in l]
    assert moved and moved[0].strip() != "html height moved: 0px", (
        f"<html>'s box changed size and the recording missed it:\n{recorded}")
    assert phone.errors == []


def test_the_build_stamp_is_shown_so_a_stale_cache_is_visible(phone):
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    line = [l for l in phone.page.inner_text("#set-diag").splitlines()
            if l.startswith("build:")]
    assert line and line[0].split(":", 1)[1].strip(), "no build identifier shown"
    assert phone.errors == []

