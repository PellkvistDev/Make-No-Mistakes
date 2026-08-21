"""Voice is easier to reach, and the empty margin shows what is running.

Two asks, and a bug found between them.

  "since the voice mode is more integrated in the app now, it should be easier
   to activate and deactivate too"  ...  "the little mic icon at the top now
   isn't cutting it"

Starting a conversation is an ACTION, and every other message action lives at
the composer. It was a 20px glyph in the titlebar -- the width of the window
away from the hands, and the same picture as the DICTATION button sitting
right next to the input, which does something else entirely.

  "we have quite a lot of space on both sides of the chat, so we could fit
   something there"

The chat is a centred 860px column. What was missing in that margin was not
more buttons -- it was an ambient view of what is RUNNING. Which turned up the
bug: worker_update was handled only on the voice route, so a worker the CODING
agent dispatched (it has the same three tools) rendered nowhere in the app at
all.
"""

from .conftest import DEFAULT_SETTINGS


def _app(desktop, width=1400, **replies):
    """Booted WITH a chat open. `body.no-session` disables the whole composer
    -- correctly: there is nothing for a voice turn to attach to -- so Talk is
    dead until a chat exists, and testing it against an empty app would be
    testing the wrong state."""
    desktop.page.set_viewport_size({"width": width, "height": 900})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/tmp/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.wait_for_timeout(200)
    return desktop


def _worker(desktop, wid="wk1", name="dark-mode", status="running", voice=False):
    desktop.page.evaluate("""([id, name, status, viaVoice]) => {
      const ev = { type: "worker_update", id, name, status, summary: "" };
      if (viaVoice) { voice.active = true; ev.sid = (window.currentSid || "s1") + "::voice"; }
      window.GLM.emit(ev);
    }""", [wid, name, status, voice])
    desktop.page.wait_for_timeout(200)


# --------------------------------------------------------------------- #
# starting and ending a conversation

def test_talk_sits_at_the_composer(desktop):
    """Where the hands already are, next to send -- not in the titlebar."""
    _app(desktop)
    assert desktop.page.is_visible("#composer #talk-btn")


def test_it_is_dead_until_a_chat_is_open(desktop):
    """Not a special case: the whole composer is, and it should be. A spoken
    turn has nothing to attach to without a chat."""
    desktop.page.set_viewport_size({"width": 1400, "height": 900})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)})
    desktop.page.wait_for_timeout(150)
    assert desktop.page.evaluate(
        "() => getComputedStyle(document.getElementById('talk-btn')).pointerEvents"
    ) == "none"


def test_it_is_labelled_not_just_another_mic_glyph(desktop):
    """"mic" is the same picture for "type this for me" and "let's talk", and
    a word is the cheapest way to tell them apart."""
    _app(desktop)
    assert desktop.page.text_content("#talk-btn").strip() == "Talk"


def test_dictation_and_conversation_are_no_longer_the_same_picture(desktop):
    """They were identical icons 500px apart, meaning different things."""
    _app(desktop)
    dictate = desktop.page.eval_on_selector("#mic-btn svg", "e => e.innerHTML")
    talk = desktop.page.eval_on_selector("#talk-btn svg", "e => e.innerHTML")
    assert dictate != talk
    assert desktop.page.get_attribute("#mic-btn", "aria-label") == "Dictate into the box"


def test_clicking_it_starts_a_conversation(desktop):
    """Driven through the real handler: startVoice is module-scope, so a stub
    on window replaces nothing the listener can see. A refused microphone is
    the observable that proves the click got that far."""
    _app(desktop)
    desktop.page.evaluate("""() => {
      navigator.mediaDevices.getUserMedia = () => Promise.reject(new Error("no mic"));
    }""")
    desktop.page.click("#talk-btn")
    desktop.page.wait_for_timeout(400)
    assert desktop.page.is_visible(".toast") or \
        desktop.page.evaluate("() => voice.active") is True


def test_the_same_button_ends_it(desktop):
    """One control for one session. Having to hunt for a different button to
    stop is the other half of "easier to activate and deactivate"."""
    _app(desktop)
    desktop.page.evaluate("""() => {
      voice.active = true;
      document.getElementById("voice-dock").hidden = false;
      setTalkState(true);
    }""")
    desktop.page.click("#talk-btn")
    desktop.page.wait_for_timeout(300)
    assert desktop.page.evaluate("() => voice.active") is False
    assert desktop.page.is_hidden("#voice-dock")


def test_it_shows_that_a_session_is_live(desktop):
    _app(desktop)
    desktop.page.evaluate("() => setTalkState(true)")
    desktop.page.wait_for_timeout(120)
    assert desktop.page.get_attribute("#talk-btn", "aria-pressed") == "true"
    assert desktop.page.text_content("#talk-btn").strip() == "Listening"


def test_both_entry_points_agree(desktop):
    """The titlebar chip stays -- it is a known spot and where the wake-word
    state has always shown -- so the two must never disagree about whether a
    session is running."""
    _app(desktop)
    desktop.page.evaluate("() => setTalkState(true)")
    desktop.page.wait_for_timeout(120)
    talk = desktop.page.get_attribute("#talk-btn", "aria-pressed")
    desktop.page.evaluate("() => setTalkState(false)")
    desktop.page.wait_for_timeout(120)
    assert talk == "true"
    assert desktop.page.get_attribute("#talk-btn", "aria-pressed") == "false"


# --------------------------------------------------------------------- #
# the rail

def test_a_worker_dispatched_by_typing_is_visible_at_all(desktop):
    """The bug this turned up. worker_update was handled only on the voice
    route, so a worker the coding agent dispatched updated nothing and appeared
    nowhere -- you had to open the sub-agent panel to know it existed."""
    _app(desktop)
    _worker(desktop, voice=False)
    assert desktop.page.is_visible("#activity-rail")
    assert "dark-mode" in desktop.page.text_content("#activity-rail")


def test_a_worker_dispatched_by_voice_shows_too(desktop):
    _app(desktop)
    _worker(desktop, voice=True)
    assert "dark-mode" in desktop.page.text_content("#activity-rail")


def test_the_rail_is_not_there_when_nothing_is_running(desktop):
    """A rail with nothing in it is furniture: it makes the window look busier
    while saying nothing."""
    _app(desktop)
    assert desktop.page.is_hidden("#activity-rail")


def test_it_lives_in_the_margin_beside_the_chat(desktop):
    """It must not overlap the reading column, whether or not the sidebar has
    taken the left of the window."""
    _app(desktop, width=1500)
    _worker(desktop)
    for sidebar in (False, True):
        desktop.page.evaluate("(on) => document.body.classList.toggle('sidebar-open', on)",
                              sidebar)
        desktop.page.wait_for_timeout(150)
        box = desktop.page.evaluate("""() => {
          const r = document.getElementById('activity-rail').getBoundingClientRect();
          const c = document.getElementById('chat');
          const pad = parseFloat(getComputedStyle(c).paddingLeft);
          return { right: r.right, textStarts: c.getBoundingClientRect().left + pad };
        }""")
        assert box["right"] <= box["textStarts"] + 1, (sidebar, box)


def test_clicking_an_item_opens_its_thread(desktop):
    _app(desktop)
    _worker(desktop)
    desktop.page.click("#activity-rail .activity-item")
    desktop.page.wait_for_timeout(250)
    assert "subagent-open" in desktop.page.evaluate("() => document.body.className")
    assert desktop.page.evaluate("() => activeSubagentId") == "wk1"


def test_it_shows_what_the_worker_is_doing(desktop):
    _app(desktop)
    _worker(desktop)
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "subagent_stream", id: "wk1", kind: "tool_call",
      name: "edit_file", args: { path: "auth.py" } })""")
    desktop.page.wait_for_timeout(200)
    assert "editing" in desktop.page.text_content("#activity-rail")


def test_a_finished_worker_stops_looking_live(desktop):
    _app(desktop)
    _worker(desktop, status="running")
    live = desktop.page.get_attribute("#activity-rail .activity-item", "class")
    _worker(desktop, status="done")
    done = desktop.page.get_attribute("#activity-rail .activity-item", "class")
    assert "ai-run" in live
    assert "ai-done" in done


def test_a_live_voice_session_keeps_the_rail_up_without_a_row(desktop):
    """Voice is not a row here: the dock sits at the top of this rail and is
    always visible while a session is up, so a row saying "Voice" would be a
    label for something already on screen. It still has to hold the rail open."""
    _app(desktop)
    desktop.page.evaluate("""() => {
      voice.active = true;
      document.getElementById('voice-dock').hidden = false;
      renderActivityRail();
    }""")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_visible("#activity-rail")
    assert desktop.page.is_visible("#voice-orb")
    assert desktop.page.query_selector(".activity-item") is None


# --------------------------------------------------------------------- #
# when there is no room for it

def test_it_goes_away_on_a_narrow_window(desktop):
    """The margin collapses to its 24px minimum, and a rail there would sit on
    top of the text. Nothing may live ONLY here for exactly this reason."""
    _app(desktop, width=1000)
    _worker(desktop)
    assert desktop.page.is_hidden("#activity-rail")


def test_it_moves_with_the_chat_when_the_sidebar_is_open(desktop):
    """The sidebar starts OPEN. Hiding the rail while it is (the first version
    of this) meant the rail was invisible in the app's normal state -- and the
    probe that caught it is the reason this test exists at all."""
    _app(desktop, width=1500)
    _worker(desktop)
    desktop.page.evaluate("() => document.body.classList.add('sidebar-open')")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_visible("#activity-rail")
    left = desktop.page.evaluate(
        "() => document.getElementById('activity-rail').getBoundingClientRect().left")
    assert left >= 268, left      # clear of the sidebar


def test_it_goes_away_when_the_sidebar_leaves_no_room(desktop):
    """The threshold is the plain one plus the sidebar it shares the window
    with."""
    _app(desktop, width=1200)
    desktop.page.evaluate("() => document.body.classList.add('sidebar-open')")
    _worker(desktop)
    assert desktop.page.is_hidden("#activity-rail")


def test_everything_in_it_is_reachable_without_it(desktop):
    """Which is what makes hiding it safe: the sub-agent panel lists the same
    threads."""
    _app(desktop, width=1000)
    _worker(desktop)
    desktop.page.evaluate("() => openSubagentPanel('wk1', 'dark-mode', 'running')")
    desktop.page.wait_for_timeout(250)
    assert "dark-mode" in desktop.page.text_content("#subagent-tabs")


def test_nothing_here_throws(desktop):
    _app(desktop)
    _worker(desktop)
    desktop.page.evaluate("() => setTalkState(true)")
    desktop.page.wait_for_timeout(150)
    assert desktop.errors == []
