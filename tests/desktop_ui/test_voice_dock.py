"""Voice is a dock in the corner, not a screen over the app.

Three complaints, one shape:

  - "there's no reason for that box to cover the whole screen"
  - "there's no way to look manually on subagents that started in voice mode"
  - "the chat is now displayed in the normal chat, and also in the voice pop up"

They are the same problem seen from three sides. The panel was
`position: fixed; inset: 0` with a scrim and aria-modal, so turning voice on
took the app away -- and the one thing you most want while it works, the
sub-agent inspector, was the thing it covered. The transcript in it was a
second copy of a conversation that now lives in the real chat, and carrying
that copy is most of why it had to be screen-sized at all.
"""

from .conftest import DEFAULT_SETTINGS


def _voice(desktop, **replies):
    """Bring the dock up the way the app does, without a microphone."""
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("() => { document.getElementById('voice-dock').hidden = false; }")
    desktop.page.wait_for_timeout(150)
    return desktop


def _seen(desktop, sel):
    return desktop.page.evaluate(
        """(s) => { const e = document.querySelector(s);
                    if (!e) return null;
                    const r = e.getBoundingClientRect();
                    return { w: r.width, h: r.height, left: r.left, right: r.right,
                             top: r.top, bottom: r.bottom }; }""", sel)


# --------------------------------------------------------------------- #
# it does not cover the app

def test_the_dock_is_a_corner_card_not_a_full_screen_panel(desktop):
    _voice(desktop)
    box = _seen(desktop, "#voice-dock")
    vw, vh = desktop.page.evaluate("() => [innerWidth, innerHeight]")
    assert box["w"] < vw * 0.45, box
    assert box["h"] < vh * 0.8, box


def test_there_is_no_scrim_over_the_app(desktop):
    """The scrim is what made everything behind it unreachable, and it is also
    what made the app LOOK unavailable while you talked."""
    _voice(desktop)
    bg = desktop.page.evaluate(
        "() => getComputedStyle(document.getElementById('voice-dock')).backgroundColor")
    assert bg in ("rgba(0, 0, 0, 0)", "transparent"), bg


def test_it_is_not_a_modal(desktop):
    """aria-modal tells a screen reader the rest of the app is gone. It isn't."""
    _voice(desktop)
    card = desktop.page.query_selector("#voice-dock [role]")
    assert card.get_attribute("aria-modal") is None
    assert card.get_attribute("role") == "region"


def test_the_space_around_the_card_does_not_eat_clicks(desktop):
    """The dock spans a column of the window; only the CARD may take a click,
    or the corner of the app silently stops responding."""
    _voice(desktop)
    assert desktop.page.evaluate(
        "() => getComputedStyle(document.getElementById('voice-dock')).pointerEvents"
    ) == "none"
    assert desktop.page.evaluate(
        "() => getComputedStyle(document.querySelector('.voice-card')).pointerEvents"
    ) == "auto"


def test_the_chat_is_still_there_behind_it(desktop):
    _voice(desktop)
    assert desktop.page.is_visible("#chat")
    assert desktop.page.is_visible("#composer")


def test_settings_still_opens_over_it(desktop):
    """It sits below --z-sheet on purpose: Settings is reachable while talking,
    and the voice panel's own Settings button depends on that."""
    _voice(desktop)
    z = desktop.page.evaluate(
        "() => +getComputedStyle(document.getElementById('voice-dock')).zIndex")
    assert z < 50, z


# --------------------------------------------------------------------- #
# the conversation is not printed twice

def test_the_dock_holds_the_latest_exchange_not_a_log(desktop):
    """Spoken turns reach the real chat now (voice_chat_turn), so a scrolling
    copy here was the same conversation twice."""
    _voice(desktop)
    desktop.page.evaluate("""() => {
      for (let i = 0; i < 6; i++) {
        addVoiceTurn("turn " + i, false);
        voiceReplyEl().textContent = "reply " + i;
      }
    }""")
    kept = desktop.page.eval_on_selector_all(
        "#voice-caption .voice-turn .voice-you", "els => els.map(e => e.textContent)")
    assert len(kept) <= 2, kept
    assert kept[-1] == "turn 5"          # the newest is the one kept


def test_a_spoken_turn_still_reaches_the_real_chat(desktop):
    """Which is what makes dropping the log safe. If this ever stopped, the
    dock would be the only record and it keeps only the last exchange."""
    _voice(desktop)
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "voice_chat_turn", user: "add dark mode", assistant: "done, added it" })""")
    desktop.page.wait_for_timeout(150)
    said = desktop.page.text_content("#chat")
    assert "add dark mode" in said
    assert "done, added it" in said


# --------------------------------------------------------------------- #
# a worker dispatched by voice can be looked at

def _dispatch(desktop, wid="wk1", name="dark-mode", status="running"):
    """Through the real routing: voice events carry a "<sid>::voice" sid, and
    that tag is what sends them to the dock instead of the coding transcript."""
    desktop.page.evaluate("""([id, name, status]) => {
      voice.active = true;
      window.GLM.emit({ sid: (window.currentSid || "s1") + "::voice",
                        type: "worker_update", id, name, status, summary: "" });
    }""", [wid, name, status])
    desktop.page.wait_for_timeout(200)


def test_a_voice_worker_is_shown_as_a_pill(desktop):
    _voice(desktop)
    _dispatch(desktop)
    assert "dark-mode" in desktop.page.text_content("#voice-workers")


def test_clicking_a_worker_opens_its_thread(desktop):
    """The whole second complaint. A worker's id IS its sub-agent id, so the
    pill can open the same inspector everything else uses -- which used to be
    behind the screen the voice panel was covering it with."""
    _voice(desktop)
    _dispatch(desktop)
    desktop.page.click("#voice-workers .voice-worker")
    desktop.page.wait_for_timeout(250)
    assert "subagent-open" in desktop.page.evaluate("() => document.body.className")
    assert desktop.page.evaluate("() => activeSubagentId") == "wk1"


def test_a_worker_pill_is_a_real_button(desktop):
    """It was a div. Nothing about it said it could be clicked, and nothing
    let a keyboard reach it."""
    _voice(desktop)
    _dispatch(desktop)
    tag = desktop.page.evaluate(
        "() => document.querySelector('#voice-workers .voice-worker').tagName")
    assert tag == "BUTTON"


def test_the_dock_steps_aside_for_the_inspector(desktop):
    """They live in the same corner, and clicking a pill is what opens the
    inspector -- so they would collide on the one interaction that matters."""
    _voice(desktop)
    _dispatch(desktop)
    before = _seen(desktop, ".voice-card")["right"]
    desktop.page.click("#voice-workers .voice-worker")
    desktop.page.wait_for_timeout(450)
    after = _seen(desktop, ".voice-card")["right"]
    assert after < before, (before, after)
    panel = _seen(desktop, "#subagent-panel")
    assert after <= panel["left"] + 1, (after, panel)


# --------------------------------------------------------------------- #
# out of the way entirely

def test_it_can_collapse_to_just_the_orb(desktop):
    """The state the full-screen panel could not have at all: listening, and
    entirely out of the way."""
    _voice(desktop)
    wide = _seen(desktop, ".voice-card")["w"]
    desktop.page.click("#voice-collapse")
    desktop.page.wait_for_timeout(200)
    assert _seen(desktop, ".voice-card")["w"] < wide
    assert desktop.page.is_visible("#voice-orb")
    assert desktop.page.is_visible("#voice-close")
    assert not desktop.page.is_visible("#voice-caption")


def test_collapsing_is_reversible(desktop):
    _voice(desktop)
    desktop.page.click("#voice-collapse")
    desktop.page.wait_for_timeout(150)
    desktop.page.click("#voice-collapse")
    desktop.page.wait_for_timeout(150)
    assert "collapsed" not in desktop.page.evaluate(
        "() => document.getElementById('voice-dock').className")


def test_nothing_here_throws(desktop):
    _voice(desktop)
    _dispatch(desktop)
    desktop.page.click("#voice-collapse")
    desktop.page.wait_for_timeout(150)
    assert desktop.errors == []
