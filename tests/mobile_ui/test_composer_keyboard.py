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
# keyboard for text fields. It belongs to the system, there is no web API to
# remove it, and visualViewport does not count it -- the viewport shrinks by
# the keyboard alone. So the composer was positioned correctly and still sat
# underneath it, invisible exactly while being typed into.

def kb_on(phone, app_url, platform, covered=300):
    """Boot the app as if on `platform`, then pretend the keyboard covers
    `covered` px, and report what --kb ended up as.

    The platform has to be set BEFORE the app loads: whether the accessory bar
    exists is decided once at startup, which is right -- a phone does not stop
    being a phone mid-session -- but it does mean an override applied afterwards
    would be measuring nothing. (First version of this test did exactly that and
    failed against correct code.)
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


def test_the_dock_clears_ios_accessory_bar_as_well_as_the_keyboard(phone, app_url):
    assert kb_on(phone, app_url, "iPhone") == "344px", \
        "the dock only cleared the keys, leaving the composer under iOS's own bar"
    assert phone.errors == []


def test_platforms_without_that_bar_do_not_get_a_dead_gap(phone, app_url):
    """The clearance is for a bar that only exists on iOS. Anywhere else it
    would just be 44px of nothing between the composer and the keyboard."""
    assert kb_on(phone, app_url, "Linux x86_64") == "300px"
    assert phone.errors == []


def test_a_collapsing_address_bar_is_still_not_mistaken_for_a_keyboard(phone, app_url):
    """The small-delta guard has to run before the accessory clearance, or a
    60px browser chrome change would become a 104px phantom keyboard."""
    assert kb_on(phone, app_url, "iPhone", covered=60) == "0px"
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
    assert got == "344px", (
        "the dock did not lift: the measurement used a reference that shrinks "
        f"with the keyboard, so it saw nothing covered (--kb = {got})")
    assert phone.errors == []


def test_the_same_holds_where_there_is_no_accessory_bar(phone, app_url):
    got = kb_when_inner_height_also_shrinks(phone, app_url, "Linux x86_64")
    assert got == "300px", f"--kb = {got}"
    assert phone.errors == []


# ------------------------------------------------------------ diagnostics --
# The keyboard took three attempts because every number that would have
# answered it lives on the device and nowhere else. They are in the app now.

def test_settings_reports_the_numbers_that_describe_this_screen(phone):
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    text = phone.page.inner_text("#set-diag")
    for key in ("build", "standalone", "layout h", "inner h", "visual h",
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


def test_the_build_stamp_is_shown_so_a_stale_cache_is_visible(phone):
    phone.setup()
    phone.page.click("#btn-chat-settings")
    phone.page.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    line = [l for l in phone.page.inner_text("#set-diag").splitlines()
            if l.startswith("build:")]
    assert line and line[0].split(":", 1)[1].strip(), "no build identifier shown"
    assert phone.errors == []
