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


def test_the_document_is_tall_enough_to_paint_the_strip_but_still_cannot_scroll(phone):
    """The whole fix for the bottom of an iOS PWA rests on this combination.

    <html> is deliberately taller than the viewport (min-height: 100% + the top
    inset) so its canvas background paints the strip of glass that sits below
    the shifted layout viewport -- nothing else reaches down there. That extra
    height is exactly what iOS previously turned into scroll, sliding the whole
    UI up. Keeping the height and removing the scroll is the point, so assert
    both halves together: if a later edit drops overflow:hidden, the height
    silently becomes a scroll again and the shove comes back.

    The inset is 0 in a headless browser, so simulate it at the same magnitude
    rather than asserting on a value that is always zero here.
    """
    phone.setup()
    p = phone.page
    taller = p.evaluate("""() => {
      const css = document.createElement('style');
      css.id = 'inset-sim';
      css.textContent = 'html { min-height: calc(100% + 60px) !important; }';
      document.head.appendChild(css);
      const h = document.documentElement;
      return h.scrollHeight > h.clientHeight;
    }""")
    assert taller, "the document is no longer tall enough to cover the strip"

    # overflow:hidden on the root does NOT hold this on its own -- Chromium
    # honours the scroll anyway, and iOS scrolls to reveal a focused field. The
    # guard is a scroll listener, so it settles a frame later, not instantly.
    p.evaluate("() => window.scrollTo(0, 60)")
    p.wait_for_function("() => (window.scrollY || document.documentElement.scrollTop || 0) === 0",
                        timeout=3000)
    p.evaluate("() => document.getElementById('inset-sim').remove()")
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

    Only the root canvas can reach the strip of screen below the layout
    viewport in an installed iOS PWA -- a position:fixed layer cannot, which
    was tried and did nothing. So the canvas has to paint it, and anything else
    painting the same image over the viewport re-creates the mismatch. This is
    the invariant that makes the seam impossible rather than merely tuned away.
    """
    with_wallpaper(phone, app_url, {"type": "color", "value": GRADIENT})
    who = phone.page.evaluate("""() => {
      const cs = (el) => getComputedStyle(el);
      return { root: cs(document.documentElement).backgroundImage,
               layer: cs(document.getElementById('bg-layer')).backgroundImage,
               body: cs(document.body).backgroundColor };
    }""")
    assert who["root"].startswith("linear-gradient"), f"the canvas is not painting it: {who}"
    assert who["layer"] == "none", f"#bg-layer paints it too -- that is the seam: {who}"
    # An opaque body would hide the canvas over the viewport and leave it
    # showing only in the strip, which is the same two-surface split again.
    assert who["body"] in ("rgba(0, 0, 0, 0)", "transparent"), \
        f"body hides the canvas over the viewport: {who}"
    assert phone.errors == []


def test_a_gradient_preset_actually_applies(phone, app_url):
    """applyBg set the `background` shorthand and then cleared background-image
    immediately after, which wiped out the gradient it had just set. Photo
    wallpapers passed an explicit image so they survived; gradient presets came
    out blank."""
    with_wallpaper(phone, app_url, {"type": "color", "value": GRADIENT})
    painted = phone.page.evaluate(
        "() => getComputedStyle(document.documentElement).backgroundImage")
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


def test_focusing_a_field_removes_the_slack_before_anything_can_scroll_it(phone):
    """The reported flash: the UI lurched up and snapped back on every tap.

    The scroll listener fixed the position but a frame too late, so the lurch
    was visible. The document's only scrollable slack is the extra inset of
    height on <html>; dropping it on focusin means there is nothing to scroll,
    so nothing to correct and nothing to see.
    """
    phone.setup()
    p = phone.page
    simulate_insets(phone)

    before = p.evaluate("() => document.documentElement.scrollHeight - "
                        "document.documentElement.clientHeight")
    assert before > 0, "no slack to begin with — this test would prove nothing"

    p.click("#in-prompt")
    p.wait_for_function("() => document.documentElement.classList.contains('kb-open')",
                        timeout=5000)
    during = p.evaluate("() => document.documentElement.scrollHeight - "
                        "document.documentElement.clientHeight")
    assert during <= 1, f"the document can still be scrolled while typing ({during}px)"

    # And it comes back once the field is left, so the strip is painted again.
    p.evaluate("() => document.getElementById('in-prompt').blur()")
    p.wait_for_function("() => !document.documentElement.classList.contains('kb-open')",
                        timeout=5000)
    after = p.evaluate("() => document.documentElement.scrollHeight - "
                       "document.documentElement.clientHeight")
    assert after == before, f"the height didn't come back after blur ({after} vs {before})"
    assert phone.errors == []


def test_moving_between_two_fields_does_not_restore_the_slack_in_between(phone):
    """focusout fires before focusin on the next field. Restoring immediately
    would hand the slack back for a frame — the same flash, in miniature."""
    phone.setup()
    p = phone.page
    simulate_insets(phone)
    p.click("#in-prompt")
    p.wait_for_function("() => document.documentElement.classList.contains('kb-open')",
                        timeout=5000)
    # Settings has its own text fields; move focus straight to one.
    p.click("#btn-chat-settings")
    p.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
    p.evaluate("() => document.getElementById('set-model').focus()")
    p.wait_for_timeout(120)
    assert p.evaluate("() => document.documentElement.classList.contains('kb-open')"), \
        "the slack came back while moving between two fields"
    assert phone.errors == []

