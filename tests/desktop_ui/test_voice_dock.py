"""The voice dock: the orb, mute, the mode, and push-to-talk. Nothing else.

  "The box should always be compact, and be above the tasks on the left. It
   should just be the orb, mute button, a button which cycles through the modes
   (handsfree, push to talk, wake word) and a push to talk button when that is
   needed. Nothing else is needed there."

Everything that used to be in it went somewhere better rather than away: the
transcript is the chat, the workers are the rail directly underneath it, and
ending the session is the Talk button at the composer. What is left is the
handful of things you reach for WHILE talking -- which is why there is no
collapsed state any more, and no floating position of its own. This IS the
small state, and the rail places it.

The permission card is the one thing kept beyond that list, and it is
transient: a gated action has to be approvable, and "just say yes" is not
always heard.
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

def test_the_dock_is_a_small_card_not_a_full_screen_panel(desktop):
    _voice(desktop)
    box = _seen(desktop, ".voice-card")
    vw, vh = desktop.page.evaluate("() => [innerWidth, innerHeight]")
    assert box["w"] < vw * 0.2, box
    assert box["h"] < vh * 0.2, box


def test_there_is_no_scrim_over_the_app(desktop):
    """The scrim is what made everything behind it unreachable, and it is also
    what made the app LOOK unavailable while you talked."""
    _voice(desktop)
    bg = desktop.page.evaluate(
        "() => getComputedStyle(document.getElementById('activity-rail')).backgroundColor")
    assert bg in ("rgba(0, 0, 0, 0)", "transparent"), bg

def test_nothing_here_throws(desktop):
    _voice(desktop)
    _dispatch(desktop)
    desktop.page.click("#voice-collapse")
    desktop.page.wait_for_timeout(150)
    assert desktop.errors == []

from .conftest import DEFAULT_SETTINGS


def _voice(desktop, width=1400, **replies):
    desktop.page.set_viewport_size({"width": width, "height": 900})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/tmp/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.evaluate("""() => {
      voice.active = true;
      document.getElementById('voice-dock').hidden = false;
      renderVoiceMode();
      renderActivityRail();
    }""")
    desktop.page.wait_for_timeout(200)
    return desktop


# --------------------------------------------------------------------- #
# what is in it

def test_it_holds_the_orb_mute_and_the_mode(desktop):
    _voice(desktop)
    assert desktop.page.is_visible("#voice-orb")
    assert desktop.page.is_visible("#voice-mute")
    assert desktop.page.is_visible("#voice-mode")


def test_and_nothing_else(desktop):
    """The list was exhaustive. Each of these went somewhere better: the
    transcript to the chat, the workers to the rail below, ending the session
    to the Talk button at the composer."""
    _voice(desktop)
    for gone in ("voice-caption", "voice-wave", "voice-status", "voice-close",
                 "voice-collapse", "voice-replay", "voice-settings",
                 "voice-workers", "voice-gate-toggle", "voice-ptt-toggle"):
        assert desktop.page.query_selector("#" + gone) is None, gone


def test_push_to_talk_appears_only_in_that_mode(desktop):
    """"...and a push to talk button when that is needed." """
    _voice(desktop)
    assert desktop.page.is_hidden("#voice-ptt-btn")
    desktop.page.evaluate("() => setVoicePtt(true)")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_visible("#voice-ptt-btn")


def test_the_permission_card_is_still_reachable(desktop):
    """The one thing kept beyond the list, and it is transient: a gated action
    has to be approvable, and "just say yes" is not always heard."""
    _voice(desktop)
    assert desktop.page.query_selector("#voice-perm") is not None
    assert desktop.page.is_hidden("#voice-perm")      # not part of the resting size


# --------------------------------------------------------------------- #
# always compact, above the tasks

def test_it_is_small_and_stays_small(desktop):
    _voice(desktop)
    box = desktop.page.evaluate(
        "() => document.querySelector('.voice-card').getBoundingClientRect()")
    assert box["height"] < 120, box
    assert box["width"] < 220, box


def test_there_is_no_collapsed_state_to_get_into(desktop):
    """It cannot be made smaller or bigger, so there is nothing to remember,
    reset, or get stuck in."""
    _voice(desktop)
    assert desktop.page.query_selector("#voice-collapse") is None
    assert "collapsed" not in desktop.page.evaluate(
        "() => document.getElementById('voice-dock').className")


def test_it_sits_above_the_tasks(desktop):
    _voice(desktop)
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "worker_update", id: "wk1", name: "dark-mode", status: "running" })""")
    desktop.page.wait_for_timeout(200)
    pos = desktop.page.evaluate("""() => {
      const d = document.querySelector('.voice-card').getBoundingClientRect();
      const t = document.querySelector('.activity-item').getBoundingClientRect();
      return { dockBottom: d.bottom, taskTop: t.top, dockLeft: d.left, taskLeft: t.left };
    }""")
    assert pos["dockBottom"] <= pos["taskTop"] + 1, pos
    assert abs(pos["dockLeft"] - pos["taskLeft"]) < 2, pos


def test_it_is_on_the_left_not_over_the_chat(desktop):
    _voice(desktop)
    box = desktop.page.evaluate("""() => {
      const d = document.querySelector('.voice-card').getBoundingClientRect();
      const c = document.getElementById('chat');
      const pad = parseFloat(getComputedStyle(c).paddingLeft);
      return { right: d.right, textStarts: c.getBoundingClientRect().left + pad };
    }""")
    assert box["right"] <= box["textStarts"] + 1, box


def test_the_rail_is_up_for_voice_even_with_no_work_running(desktop):
    """The dock lives in the rail now, so an active session has to keep it up
    on its own -- otherwise starting a conversation would show nothing."""
    _voice(desktop)
    assert desktop.page.is_visible("#activity-rail")
    assert desktop.page.query_selector(".activity-item") is None


# --------------------------------------------------------------------- #
# one control, three modes

def test_the_mode_button_names_the_mode(desktop):
    _voice(desktop)
    assert desktop.page.text_content("#voice-mode").strip() == "hands-free"


def test_it_cycles_through_all_three(desktop):
    """It was two independent toggles which between them could express the same
    mode two ways, and neither of them named the third."""
    _voice(desktop)
    seen = [desktop.page.text_content("#voice-mode").strip()]
    for _ in range(3):
        desktop.page.click("#voice-mode")
        desktop.page.wait_for_timeout(150)
        seen.append(desktop.page.text_content("#voice-mode").strip())
    assert seen == ["hands-free", "push-to-talk", "wake word", "hands-free"]


def test_each_mode_sets_both_underlying_flags(desktop):
    """voice.ptt and voice.gated are what the audio path actually reads. Going
    from push-to-talk to wake word has to clear one AND set the other; setting
    only the one that "changed" is how two toggles left the pair in a state
    neither of them showed."""
    _voice(desktop)
    got = {}
    for _ in range(3):
        desktop.page.click("#voice-mode")
        desktop.page.wait_for_timeout(150)
        got[desktop.page.text_content("#voice-mode").strip()] = desktop.page.evaluate(
            "() => [voice.ptt, voice.gated]")
    assert got["push-to-talk"] == [True, False]
    assert got["wake word"] == [False, True]
    assert got["hands-free"] == [False, False]


def test_the_button_follows_a_mode_set_from_elsewhere(desktop):
    """Settings and the wake-word path both change these flags directly. The
    button must not be able to claim a mode the audio path is not in."""
    _voice(desktop)
    desktop.page.evaluate("() => setVoiceGated(true)")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.text_content("#voice-mode").strip() == "wake word"
    desktop.page.evaluate("() => setVoicePtt(true)")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.text_content("#voice-mode").strip() == "push-to-talk"


def test_push_to_talk_follows_the_cycle(desktop):
    _voice(desktop)
    desktop.page.click("#voice-mode")           # -> push-to-talk
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_visible("#voice-ptt-btn")
    desktop.page.click("#voice-mode")           # -> wake word
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_hidden("#voice-ptt-btn")


def test_mute_still_works(desktop):
    _voice(desktop)
    desktop.page.click("#voice-mute")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.evaluate("() => voice.muted") is True


def test_nothing_here_throws(desktop):
    _voice(desktop)
    desktop.page.click("#voice-mode")
    desktop.page.click("#voice-mute")
    desktop.page.wait_for_timeout(200)
    assert desktop.errors == []
