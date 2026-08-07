"""Layout invariants that phone screenshots kept catching before tests did.

Three reported bugs, all of them things a headless browser can check:

  * the commit sheet grew wider than the screen when the file path (or the
    content preview) had no wrap opportunity, pushing the confirm button off
    the right edge -- so the sheet could not be accepted at all;
  * a band of wallpaper-less background at the bottom of an installed PWA,
    exactly as tall as the top safe-area inset, because the document had that
    much scrollable overflow and iOS used it;
  * focusing the composer shoving the entire UI, wallpaper included, up off
    the screen -- the same overflow, scrolled by the browser to reveal the
    focused field.

The last two are one root cause: the document was scrollable when nothing in
this app should ever scroll it.
"""

LONG_PATH = ("uploads/a-friendly-ai-assistant-character-with-a-tablet-"
             "standing-in-front-of-a-monitor-c36cb1-2824b5.txt")
LONG_LINE = "The image depicts a humanoid robot as the central subject, " * 6


def open_commit_sheet(phone):
    """Get the real confirm sheet up, via a real write_file tool call."""
    phone.reply(
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "w1", "type": "function", "function": {
             "name": "write_file",
             "arguments": __import__("json").dumps(
                 {"path": LONG_PATH, "content": LONG_LINE})}}]},
        {"role": "assistant", "content": "done"},
    )
    phone.send("describe the image")
    phone.page.wait_for_selector("#confirm-backdrop:not([hidden])", timeout=15000)


def test_the_commit_sheet_fits_the_screen(phone):
    phone.setup()
    open_commit_sheet(phone)
    p = phone.page

    box = p.evaluate("""() => {
      const w = document.documentElement.clientWidth;
      const d = document.querySelector('#confirm-backdrop .dialog');
      const yes = document.getElementById('btn-confirm-yes');
      const r = d.getBoundingClientRect(), y = yes.getBoundingClientRect();
      return { w, dialogRight: r.right, dialogLeft: r.left,
               yesRight: y.right, yesLeft: y.left,
               docScrollW: document.documentElement.scrollWidth };
    }""")
    assert box["dialogRight"] <= box["w"] + 1, f"the sheet overflows the screen: {box}"
    assert box["dialogLeft"] >= -1, f"the sheet starts off-screen: {box}"
    # The one that actually broke the feature: an unreachable confirm button.
    assert box["yesRight"] <= box["w"] + 1, f"the confirm button is off-screen: {box}"
    assert box["yesLeft"] >= -1, f"the confirm button is off-screen: {box}"
    assert box["docScrollW"] <= box["w"] + 1, f"the page scrolls sideways: {box}"
    assert phone.errors == []


def test_the_long_preview_scrolls_itself_instead_of_widening_the_sheet(phone):
    """The content still has to be readable -- fitting must not mean clipping."""
    phone.setup()
    open_commit_sheet(phone)
    fits = phone.page.evaluate("""() => {
      const pre = document.getElementById('confirm-preview');
      return { scrollable: pre.scrollWidth > pre.clientWidth + 1,
               withinScreen: pre.getBoundingClientRect().right
                             <= document.documentElement.clientWidth + 1 };
    }""")
    assert fits["withinScreen"], "the preview still pushes past the screen edge"
    assert fits["scrollable"], "the long line was clipped rather than made scrollable"
    assert phone.errors == []


def test_the_document_cannot_be_scrolled_even_if_something_overflows(phone):
    """The root cause of both the bottom band and the keyboard shove.

    Asserting "nothing currently overflows" would be vacuous here: the bug
    needed a non-zero safe-area inset to appear, and a headless Chromium
    reports every inset as 0, so the old CSS passes that check too (verified --
    it did). The property worth pinning is stronger and inset-independent:
    however much overflow appears, the document still must not scroll. That is
    exactly what iOS exploited -- it found the inset-sized overflow the old
    hack created and scrolled it.
    """
    phone.setup()
    p = phone.page
    r = p.evaluate("""() => {
      const bg = () => document.getElementById('bg-layer').getBoundingClientRect().top;
      const before = bg();
      const probe = document.createElement('div');
      probe.style.cssText = 'height:2000px;width:1px';
      document.body.appendChild(probe);
      document.getElementById('in-prompt').focus();
      window.scrollTo(0, 500);
      const out = { y: window.scrollY || document.documentElement.scrollTop || 0,
                    before, after: bg() };
      probe.remove();
      return out;
    }""")
    assert r["y"] == 0, f"the document scrolled by {r['y']}px when content overflowed"
    # The visible half of the same bug: the wallpaper riding up with the page.
    assert abs(r["after"] - r["before"]) <= 1, f"the wallpaper moved: {r}"
    assert phone.errors == []


def test_the_surface_that_paints_the_strip_reaches_past_the_viewport(phone):
    """The bottom of an iOS PWA, and the reason this moved off <html>.

    The layout viewport is shorter than the screen there, so a fixed layer with
    inset:0 stops above the bottom and a strip of bare --bg shows. That is what
    "the gradient ends above the screen bottom" was. <html> used to carry the
    extra height instead -- and that height was the only thing iOS could
    scroll, which is what made focusing a field shove the UI.

    #bg-layer now takes it as an explicit height rather than inset:0. Same
    reach, no scrollable slack anywhere. The inset is 0 in a headless browser,
    so simulate one and check the layer grows with it.
    """
    phone.setup()
    p = phone.page
    grew = p.evaluate("""() => {
      const layer = document.getElementById('bg-layer');
      const before = layer.getBoundingClientRect().height;
      document.documentElement.style.setProperty('--safe-t', '60px');
      const after = layer.getBoundingClientRect().height;
      document.documentElement.style.removeProperty('--safe-t');
      return { before, after, viewport: document.documentElement.clientHeight };
    }""")
    assert grew["after"] - grew["before"] >= 59, (
        "#bg-layer does not extend past the layout viewport, so the strip below "
        f"it stays unpainted: {grew}")
    assert phone.errors == []


def test_the_document_has_no_slack_left_to_scroll(phone):
    """The other half of moving the wallpaper off <html>: with nothing to
    paint, <html> has no reason to be taller than the viewport, so it is not.
    No slack means iOS has nothing to scroll to reveal a focused field, and
    nothing has to be taken away on focus -- which is what rescaled the
    wallpaper and made the flash."""
    phone.setup()
    p = phone.page
    # With --safe-t at its headless value of 0, `calc(100% + var(--safe-t))`
    # and `100%` are the same number and this test cannot fail. Simulate the
    # inset first, or it proves nothing -- which it did, until a mutation that
    # put the old taller rule back left it green.
    simulate_insets(phone)
    box = p.evaluate("""() => {
      const h = document.documentElement;
      return { scrollH: h.scrollHeight, clientH: h.clientHeight,
               minH: getComputedStyle(h).minHeight };
    }""")
    assert box["scrollH"] <= box["clientH"] + 1, (
        f"the document is taller than the viewport, so it can be scrolled: {box}")

    # The backstop stays regardless: anything that does overflow gets pinned.
    p.evaluate("() => window.scrollTo(0, 60)")
    p.wait_for_function("() => (window.scrollY || document.documentElement.scrollTop || 0) === 0",
                        timeout=3000)
    assert phone.errors == []


def test_focusing_a_field_does_not_resize_the_box_the_wallpaper_paints_into(phone):
    """Reported: 'the background gets shoved up and then snaps back' -- a flash
    on every tap into the composer.

    Not a scroll. The wallpaper was painted into <html>'s box and sized `cover`
    against it, and html.kb-open dropped --safe-t of min-height on focus to
    take away the only thing iOS could scroll. `cover` re-solved against the
    shorter box, so the image jumped scale on focus and back on blur. Measured
    at 903 -> 844 before this changed.

    Two fixes, each right on its own, pulling on the same number. Whatever else
    focus changes, the painted box must not move.
    """
    phone.setup()
    p = phone.page
    p.evaluate("() => document.documentElement.style.setProperty('--safe-t', '59px')")
    before = p.evaluate("() => document.getElementById('bg-layer')"
                        ".getBoundingClientRect().height")
    p.click("#in-prompt")
    p.wait_for_timeout(80)
    after = p.evaluate("() => document.getElementById('bg-layer')"
                       ".getBoundingClientRect().height")
    p.evaluate("() => document.documentElement.style.removeProperty('--safe-t')")
    assert abs(after - before) <= 1, (
        f"focusing the composer changed the wallpaper's box from {before} to "
        f"{after}; sized `cover`, that rescales the image -- the flash")
    assert phone.errors == []


GRADIENT = "linear-gradient(160deg, rgb(91, 47, 176) 0%, rgb(22, 211, 200) 100%)"


def with_wallpaper(phone, app_url, bg):
    """Boot the app with a wallpaper already chosen. applyBg runs before the
    PIN screen, so nothing needs unlocking to inspect the result."""
    phone.page.evaluate("(bg) => localStorage.setItem('mnm.bg.v1', JSON.stringify(bg))", bg)
    phone.open_at(app_url)
    phone.page.wait_for_timeout(200)


def test_exactly_one_surface_paints_the_wallpaper(phone, app_url):
    """The seam was two of them painting the same image at different scales.

    Which surface does the painting has changed -- it is #bg-layer now, not the
    root canvas, because <html>'s box is one this app resizes on focus and the
    wallpaper visibly rescaled with it. The invariant did not change: exactly
    one surface paints, so there are never two boxes to keep in agreement.
    """
    with_wallpaper(phone, app_url, {"type": "color", "value": GRADIENT})
    who = phone.page.evaluate("""() => {
      const cs = (el) => getComputedStyle(el);
      return { root: cs(document.documentElement).backgroundImage,
               layer: cs(document.getElementById('bg-layer')).backgroundImage,
               body: cs(document.body).backgroundColor };
    }""")
    assert who["layer"].startswith("linear-gradient"), f"#bg-layer is not painting it: {who}"
    assert who["root"] == "none", f"the canvas paints it too -- that is the seam: {who}"
    # An opaque body would hide the layer, leaving the wallpaper showing only
    # where body does not reach: the same two-surface split, upside down.
    assert who["body"] in ("rgba(0, 0, 0, 0)", "transparent"), \
        f"body hides the wallpaper: {who}"
    assert phone.errors == []


def test_a_gradient_preset_actually_applies(phone, app_url):
    """applyBg set the `background` shorthand and then cleared background-image
    immediately after, which wiped out the gradient it had just set. Photo
    wallpapers passed an explicit image so they survived; gradient presets came
    out blank."""
    with_wallpaper(phone, app_url, {"type": "color", "value": GRADIENT})
    painted = phone.page.evaluate(
        "() => getComputedStyle(document.getElementById('bg-layer')).backgroundImage")
    assert "gradient" in painted, f"the gradient preset did not apply: {painted!r}"
    assert phone.errors == []


# iPhone 14 Pro, the device the reports came from.
IPHONE_T, IPHONE_B = 59, 34


def simulate_insets(phone, top=IPHONE_T, bottom=IPHONE_B):
    """Give the headless browser a phone's safe areas.

    env() is always 0 here, which is why every inset-dependent bug in this file
    was found by screenshot rather than by test. The stylesheet reads the insets
    through --safe-t/--safe-b precisely so this is possible.
    """
    phone.page.evaluate("""([t, b]) => {
      const r = document.documentElement.style;
      r.setProperty('--safe-t', t + 'px');
      r.setProperty('--safe-b', b + 'px');
    }""", [top, bottom])
    phone.page.wait_for_timeout(80)


def test_the_composer_scrim_reaches_the_bottom_edge(phone):
    """The reported band. The scrim is darkest at its bottom, so cutting it off
    above the home indicator left a hard line with brighter wallpaper below --
    measured at ~33 CSS px tall in the screenshot, i.e. the bottom inset."""
    phone.setup()
    simulate_insets(phone)
    gap = phone.page.evaluate("""() => {
      const dock = document.getElementById('composer-dock');
      const cs = getComputedStyle(dock, '::before');
      return { scrimBottom: cs.bottom, dockPad: getComputedStyle(dock).paddingBottom };
    }""")
    assert gap["scrimBottom"] in ("0px", "auto"), (
        f"the scrim stops short of the dock's bottom edge, which is the band: {gap}")
    # The dock still keeps clear of the home indicator; only the scrim changed.
    assert gap["dockPad"] == f"{IPHONE_B}px", f"the dock lost its home-indicator inset: {gap}"
    assert phone.errors == []


def test_the_insets_are_readable_from_one_place(phone):
    """Guard for the indirection itself: if a later edit goes back to calling
    env() at the use site, this simulation silently stops working and every
    test built on it turns green for the wrong reason."""
    phone.setup()
    simulate_insets(phone, top=41, bottom=21)
    seen = phone.page.evaluate("""() => {
      const dock = document.getElementById('composer-dock');
      return { pad: getComputedStyle(dock).paddingBottom,
               htmlMin: getComputedStyle(document.documentElement).minHeight };
    }""")
    assert seen["pad"] == "21px", f"--safe-b is not reaching the dock: {seen}"
    assert seen["htmlMin"] != "0px", f"--safe-t is not reaching <html>: {seen}"
    assert phone.errors == []


def test_the_dock_lifts_over_the_keyboard(phone):
    """--kb is what replaces the browser's own shoving, now that the document
    is pinned. No headless keyboard exists, so drive the variable directly and
    check the dock actually rides on it."""
    phone.setup()
    p = phone.page
    rest = p.evaluate("() => document.getElementById('composer-dock').getBoundingClientRect().bottom")
    p.evaluate("() => document.documentElement.style.setProperty('--kb', '300px')")
    lifted = p.evaluate("() => document.getElementById('composer-dock').getBoundingClientRect().bottom")
    assert abs((rest - lifted) - 300) <= 2, (
        f"the dock did not clear a 300px keyboard: {rest} -> {lifted}")
    assert phone.errors == []


# ------------------------------- what sits behind iOS's translucent bar ----
# The accessory bar above the keyboard cannot be removed (see index.html), and
# it is translucent: whatever the app draws underneath shows through it. So the
# app has to draw something worth seeing there. Everything positioned against
# the dock has to account for the lift, or it stays at the bottom of the screen
# and appears through the bar in pieces.

KB = 300


def with_keyboard(phone, px=KB):
    phone.page.evaluate("(px) => document.documentElement.style.setProperty('--kb', px + 'px')", px)
    phone.page.wait_for_timeout(60)


def test_the_scrim_stays_at_the_bottom_of_the_screen_when_the_dock_lifts(phone):
    """Reported: 'the gradient that's supposed to be at the bottom of the screen
    gets shoved above that bar'. As a child of the dock the scrim rode up with
    it, so the gradient ended at the top of the accessory bar and raw chat text
    showed through the translucent bar below."""
    phone.setup()
    simulate_insets(phone)
    with_keyboard(phone)
    seen = phone.page.evaluate("""() => {
      const dock = document.getElementById('composer-dock');
      const cs = getComputedStyle(dock, '::before');
      return { position: cs.position, bottom: cs.bottom, height: parseFloat(cs.height),
               dockH: dock.offsetHeight };
    }""")
    assert seen["position"] == "fixed", (
        "the scrim is positioned against the dock, so it lifts with it and stops "
        f"at the top of the accessory bar: {seen}")
    assert seen["bottom"] == "0px", f"the scrim does not reach the screen bottom: {seen}"
    assert seen["height"] >= KB + seen["dockH"], (
        f"the scrim is too short to reach behind the keyboard: {seen}")
    assert phone.errors == []


def test_no_chat_is_left_sitting_under_the_lifted_composer(phone):
    """The other half of the same picture: .messages is fixed;inset:0, so it
    spans the part of the screen the keyboard covers. Padding only for the dock
    left a keyboard's worth of chat underneath it, which the translucent bar
    then showed in slices."""
    phone.setup()
    phone.send("first")
    phone.wait_idle()
    with_keyboard(phone)
    seen = phone.page.evaluate("""() => {
      const msgs = document.getElementById('messages');
      const dock = document.getElementById('composer-dock');
      return { pad: parseFloat(getComputedStyle(msgs).paddingBottom),
               dockH: dock.offsetHeight };
    }""")
    assert seen["pad"] >= KB + seen["dockH"], (
        f"chat can scroll under the composer and the keyboard: {seen}")
    assert phone.errors == []


def test_opening_the_keyboard_keeps_you_at_the_bottom_of_the_chat(phone):
    """Reported: at the bottom of the chat, tap the composer, and the last
    message is gone -- you have to scroll down again to see what you were
    replying to.

    Opening the keyboard adds its whole height to this list's bottom padding.
    The "were you near the bottom?" check ran after that, so someone sitting at
    the very bottom measured as a keyboard's height away from it and the
    auto-scroll never fired. The measurement has to be taken first.
    """
    phone.setup()
    for i in range(6):
        phone.reply({"role": "assistant", "content": f"reply {i} " + "padding " * 60})
        phone.send(f"message {i}")
        phone.wait_idle()
    p = phone.page
    p.evaluate("() => { const m = document.getElementById('messages');"
               "        m.scrollTop = m.scrollHeight; }")
    p.wait_for_timeout(60)
    assert p.evaluate("""() => {
      const m = document.getElementById('messages');
      return m.scrollHeight - m.scrollTop - m.clientHeight;
    }""") < 80, "test setup failed: not at the bottom before the keyboard"

    p.evaluate("""() => {
      const vv = window.visualViewport;
      const layout = document.documentElement.clientHeight;
      Object.defineProperty(vv, 'height', { configurable: true, get: () => layout - 300 });
      vv.dispatchEvent(new Event('resize'));
    }""")
    p.wait_for_timeout(80)
    gap = p.evaluate("""() => {
      const m = document.getElementById('messages');
      return m.scrollHeight - m.scrollTop - m.clientHeight;
    }""")
    assert gap < 80, (
        f"the keyboard pushed the bottom {round(gap)}px away and the view did not "
        "follow, so the message you were replying to slid off screen")
    assert phone.errors == []


def test_the_jump_to_latest_pill_rides_up_with_the_composer(phone):
    """It floats just above the dock. Anchored to the dock height alone it
    stays at the bottom of the screen, behind the keys."""
    phone.setup()
    with_keyboard(phone)
    bottom = phone.page.evaluate("""() => {
      const el = document.createElement('button');
      el.className = 'to-bottom';
      document.getElementById('screen-chat').appendChild(el);
      const v = getComputedStyle(el).bottom;
      el.remove();
      return parseFloat(v);
    }""")
    assert bottom >= KB, f"the pill sits {bottom}px up, inside the keyboard"
    assert phone.errors == []
