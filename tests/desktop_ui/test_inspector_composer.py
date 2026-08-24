"""The composer has to survive the panel this app most wants you to open.

Found by photographing a surface the first audit skipped. `body.subagent-open`
gives the inspector the right half of the window, which leaves the composer
about 320px at the default size -- and with six controls in it the textarea was
squeezed to TWENTY-TWO pixels against a placeholder needing 235.

That is not a clipped hint. It is a box you cannot type in, and you reach it by
clicking a sub-agent to watch it work.
"""

from .conftest import DEFAULT_SETTINGS


def _app(desktop, width=1280):
    desktop.page.set_viewport_size({"width": width, "height": 860})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)})
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.wait_for_timeout(200)
    return desktop


def _open_panel(desktop):
    desktop.page.evaluate("""() => {
      window.GLM.emit({type: "subagent_stream", id: "sa1", kind: "content",
                       text: "Working."});
      window.GLM.emit({type: "subagent", id: "sa1", name: "theme-research",
                       status: "running", mission: "m"});
      openSubagentPanel('sa1', 'theme-research', 'running');
    }""")
    desktop.page.wait_for_timeout(300)


def _input_width(desktop):
    return desktop.page.eval_on_selector(
        "#input", "e => e.getBoundingClientRect().width")


def _hint_width(desktop):
    return desktop.page.evaluate("""() => {
      const el = document.getElementById('input');
      const cs = getComputedStyle(el);
      const c = document.createElement('canvas').getContext('2d');
      c.font = `${cs.fontSize} ${cs.fontFamily}`;
      return c.measureText(el.placeholder).width;
    }""")


def test_you_can_still_type_with_the_inspector_open(desktop):
    """22px. Measured, not eyeballed."""
    _app(desktop)
    _open_panel(desktop)
    assert _input_width(desktop) >= 110, _input_width(desktop)


def test_talk_is_what_gets_dropped_to_make_the_room(desktop):
    """It is the one control here with a second home -- the titlebar chip opens
    the same session, and setTalkState keeps the two in step."""
    _app(desktop)
    _open_panel(desktop)
    assert desktop.page.is_hidden("#talk-btn")
    assert desktop.page.is_visible("#voice-chip")


def test_dictation_is_not_dropped(desktop):
    """Unlike Talk it has nowhere else to be, so it stays even though it costs
    room -- "nothing may live only here" cuts both ways."""
    _app(desktop)
    _open_panel(desktop)
    assert desktop.page.is_visible("#mic-btn")


def test_the_hint_fits_rather_than_being_cut_mid_word(desktop):
    """"Ask anything... (@ t" teaches nothing. If there is no room for the @
    trick, saying less beats saying half of it."""
    _app(desktop)
    _open_panel(desktop)
    assert _hint_width(desktop) <= _input_width(desktop) - 8, \
        (_hint_width(desktop), _input_width(desktop))


def test_closing_it_puts_everything_back(desktop):
    _app(desktop)
    _open_panel(desktop)
    desktop.page.evaluate("() => closeSubagentPanel()")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.is_visible("#talk-btn")
    assert "@" in desktop.page.get_attribute("#input", "placeholder")


def test_clearing_it_puts_everything_back_too(desktop):
    """The other way out -- a session switch calls this one, not close."""
    _app(desktop)
    _open_panel(desktop)
    desktop.page.evaluate("() => clearSubagentPanel()")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.is_visible("#talk-btn")
    assert "@" in desktop.page.get_attribute("#input", "placeholder")


def test_a_wide_window_keeps_the_full_hint_with_the_panel_open(desktop):
    """The swap is about the panel, and at 1800px there is room for both."""
    _app(desktop, width=1800)
    _open_panel(desktop)
    assert _hint_width(desktop) <= _input_width(desktop) - 8
