"""What the app says before it has been set up, and what it says at every size.

Found by photographing the real UI rather than by reading it. Two things were
visibly wrong in the shipped app and neither had a test:

  - The model chip in the titlebar was BLANK on first run. It is filled from
    the current chat's provider, and a brand-new user has none -- so the one
    thing they have to configure was represented by an empty pill, and the
    tooltip read "Model: undefined (via undefined)" because the fallback was
    applied only to the label.
  - The composer placeholder is longer than the box at ordinary window sizes,
    so it wrapped and the textarea's own height clipped the second line:
    "(type @ to add a fil". A hint cut off mid-word is worse than a short one.
"""

from .conftest import DEFAULT_SETTINGS


def _boot(desktop, providers=None):
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)},
                 providers=providers if providers is not None else {})
    desktop.page.wait_for_timeout(250)
    return desktop


# ------------------------------------------------- nothing set up yet ----

def test_the_model_chip_is_not_a_blank_pill(desktop):
    _boot(desktop, providers={"providers": [], "chat_model": "",
                              "chat_provider": ""})
    label = desktop.page.text_content("#model-chip-label").strip()
    assert label, "the titlebar showed an empty pill where the model goes"
    assert "model" in label.lower()


def test_it_says_what_to_do_rather_than_naming_nothing(desktop):
    _boot(desktop, providers={"providers": [], "chat_model": "",
                              "chat_provider": ""})
    title = desktop.page.get_attribute("#model-chip", "title")
    assert "undefined" not in title
    assert "click" in title.lower()


def test_it_looks_like_a_way_in(desktop):
    """An empty pill reads as something someone forgot to fill; the chip IS the
    route to setting a model, so it should look like one."""
    _boot(desktop, providers={"providers": [], "chat_model": "",
                              "chat_provider": ""})
    assert "needs-setup" in desktop.page.get_attribute("#model-chip", "class")


def test_a_configured_model_is_named_plainly(desktop):
    """And carries none of that: there is nothing to fix, so nagging would be
    wrong."""
    _boot(desktop, providers={"providers": [], "chat_model": "glm-4.7-flash",
                              "chat_provider": "z.ai"})
    assert desktop.page.text_content("#model-chip-label").strip() == "glm-4.7-flash"
    assert "needs-setup" not in desktop.page.get_attribute("#model-chip", "class")
    assert "undefined" not in desktop.page.get_attribute("#model-chip", "title")


# ------------------------------------------------- the composer hint -----

def test_the_placeholder_stays_on_one_line(desktop):
    """It wrapped, and the textarea's height clipped the second line."""
    _boot(desktop)
    assert desktop.page.evaluate(
        "() => getComputedStyle(document.getElementById('input'), '::placeholder')"
        ".whiteSpace") == "nowrap"


def test_the_hint_fits_the_box_even_mid_conversation(desktop):
    """Measured rather than eyeballed. The trigger was the Talk button widening
    for a live session, which took 60px off the input -- so this checks the
    worst case, not the resting one."""
    _boot(desktop)
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.evaluate("() => setTalkState(true)")
    desktop.page.wait_for_timeout(200)
    fits = desktop.page.evaluate("""() => {
      const el = document.getElementById('input');
      const cs = getComputedStyle(el);
      const c = document.createElement('canvas').getContext('2d');
      c.font = `${cs.fontSize} ${cs.fontFamily}`;
      return { need: c.measureText(el.placeholder).width,
               have: el.getBoundingClientRect().width };
    }""")
    assert fits["need"] <= fits["have"] - 8, fits
