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
