"""Being in voice mode should be obvious, and the turn visible while it happens.

Reported, after the compact dock landed:

  "it's kinda hard to tell I'm in voice mode, that should be clearer and more
   beautiful"  ...  "before, it was much clearer when the agent actually heard
   me and thought of an answer, now I just kinda wait there and then my prompt
   and the agent's answer pops up in the chat after a while"

Both are the same mistake, made twice. The dock is deliberately four small
controls in a margin column, and I had additionally moved the status text into
a TOOLTIP -- reasoning that a status line was a fifth thing. So the only place
the app said what it was doing was behind a hover, and the only signals that a
microphone was live were a 30px dot and a green button, both easy to miss with
your eyes on the chat.

The exchange arriving all at once is the other half: it was rendered from
`voice_chat_turn`, which Python emits at the END. Listening, thinking and
answering were all silence.
"""

from .conftest import DEFAULT_SETTINGS


def _app(desktop, **replies):
    desktop.page.set_viewport_size({"width": 1400, "height": 900})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/tmp/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.wait_for_timeout(200)
    return desktop


def _voice_on(desktop):
    desktop.page.evaluate("""() => {
      voice.active = true;
      document.getElementById('voice-dock').hidden = false;
      setTalkState(true);
      renderActivityRail();
    }""")
    desktop.page.wait_for_timeout(150)


def _voice_ev(desktop, ev):
    desktop.page.evaluate("""(ev) => {
      ev.sid = (window.currentSid || "s1") + "::voice";
      window.GLM.emit(ev);
    }""", ev)
    desktop.page.wait_for_timeout(120)


# ------------------------------------------- you can tell it is running ---

def test_the_window_says_a_microphone_is_live(desktop):
    """A ring around the whole window. The dock is small and lives in a margin;
    a state this consequential should not need finding."""
    _app(desktop)
    before = desktop.page.evaluate(
        "() => getComputedStyle(document.body, '::after').boxShadow")
    _voice_on(desktop)
    after = desktop.page.evaluate(
        "() => getComputedStyle(document.body, '::after').boxShadow")
    assert after != before
    assert after and after != "none"


def test_the_ring_never_takes_a_click(desktop):
    """It spans the viewport. Taking clicks would silently kill the app."""
    _app(desktop)
    _voice_on(desktop)
    assert desktop.page.evaluate(
        "() => getComputedStyle(document.body, '::after').pointerEvents") == "none"


def test_it_goes_when_the_session_does(desktop):
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => { voice.active = false; setTalkState(false); }")
    desktop.page.wait_for_timeout(150)
    assert "voice-on" not in desktop.page.evaluate("() => document.body.className")


# ------------------------------------- what it is doing, in words ---------

def test_the_state_is_on_screen_not_in_a_tooltip(desktop):
    """The orb is ONE animated dot for listening, thinking and speaking, so
    without the word there is nothing distinguishing them -- and a tooltip is
    not on screen."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => setVoiceStatus('Thinking…')")
    assert desktop.page.is_visible("#voice-state")
    assert "Thinking" in desktop.page.text_content("#voice-state")


def test_it_keeps_up_with_the_state(desktop):
    _app(desktop)
    _voice_on(desktop)
    for text in ("Listening…", "Thinking…", "Speaking…"):
        desktop.page.evaluate("(t) => setVoiceStatus(t)", text)
        assert text.rstrip("…") in desktop.page.text_content("#voice-state")


def test_a_long_line_does_not_widen_the_dock(desktop):
    _app(desktop)
    _voice_on(desktop)
    narrow = desktop.page.eval_on_selector(
        "#voice-dock", "e => e.getBoundingClientRect().width")
    desktop.page.evaluate(
        "() => setVoiceStatus('Needs your OK — say “yes”, “no”, or “always”')")
    desktop.page.wait_for_timeout(100)
    wide = desktop.page.eval_on_selector(
        "#voice-dock", "e => e.getBoundingClientRect().width")
    assert abs(wide - narrow) < 2, (narrow, wide)


# --------------------------------- the turn is visible while it happens ---

def _bubbles(desktop):
    """The words in each bubble.

    Read off the user bubble's own text node rather than its textContent: the
    "Spoken" note is a child of the same element, so textContent would append
    it to every line and the assertions would be about the label."""
    return desktop.page.evaluate(
        """() => ({
             you: [...document.querySelectorAll('#chat .msg-user .bubble-user')]
                    .map(e => (e.firstChild && e.firstChild.nodeType === Node.TEXT_NODE)
                              ? e.firstChild.nodeValue : e.textContent),
             it: [...document.querySelectorAll('#chat .bubble-assistant')]
                    .map(e => e.textContent) })""")


def test_the_spoken_label_survives_an_update(desktop):
    """The heard text is updated in place as partials stream in, and the label
    sits beside it in the same element."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => { spokenHeard('add a'); spokenHeard('add a dark mode'); }")
    desktop.page.wait_for_timeout(120)
    assert _bubbles(desktop)["you"] == ["add a dark mode"]
    assert desktop.page.text_content("#chat .msg-user .user-note").strip() == "Spoken"


def test_what_it_heard_appears_before_the_answer_exists(desktop):
    """The whole of the complaint. This used to appear only when Python emitted
    the finished exchange."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => submitVoiceRequest('add a dark mode')")
    desktop.page.wait_for_timeout(150)
    assert _bubbles(desktop)["you"] == ["add a dark mode"]


def test_the_reply_streams_in_rather_than_landing_finished(desktop):
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => submitVoiceRequest('add a dark mode')")
    _voice_ev(desktop, {"type": "stream_start"})
    _voice_ev(desktop, {"type": "content", "text": "Starting "})
    part = _bubbles(desktop)["it"]
    assert part and "Starting" in part[0]
    _voice_ev(desktop, {"type": "content", "text": "on that now."})
    whole = _bubbles(desktop)["it"]
    assert whole == ["Starting on that now."]


def test_the_record_reconciles_instead_of_printing_it_twice(desktop):
    """voice_chat_turn is the authoritative copy, arriving at the end for a
    turn the chat has usually already shown."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => submitVoiceRequest('add a dark mode')")
    _voice_ev(desktop, {"type": "content", "text": "Starting on that now."})
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "voice_chat_turn", sid: "s1",
      user: "add a dark mode", assistant: "Starting on that now." })""")
    desktop.page.wait_for_timeout(150)
    b = _bubbles(desktop)
    assert b["you"] == ["add a dark mode"], b
    assert b["it"] == ["Starting on that now."], b


def test_a_turn_the_chat_never_saw_is_still_rendered(desktop):
    """The reconcile must not swallow one. A turn can land with nothing on
    screen -- another device finished it, or the chat was switched away."""
    _app(desktop)
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "voice_chat_turn", sid: "s1",
      user: "what changed?", assistant: "Two files." })""")
    desktop.page.wait_for_timeout(150)
    b = _bubbles(desktop)
    assert b["you"] == ["what changed?"]
    assert b["it"] == ["Two files."]


def test_the_next_exchange_gets_its_own_bubbles(desktop):
    """The references are per turn. Held across one, the second exchange would
    overwrite the first instead of following it."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => submitVoiceRequest('first question')")
    _voice_ev(desktop, {"type": "stream_start"})
    _voice_ev(desktop, {"type": "content", "text": "first answer"})
    desktop.page.evaluate("""() => window.GLM.emit({
      type: "voice_chat_turn", sid: "s1",
      user: "first question", assistant: "first answer" })""")
    desktop.page.evaluate("() => submitVoiceRequest('second question')")
    _voice_ev(desktop, {"type": "stream_start"})
    _voice_ev(desktop, {"type": "content", "text": "second answer"})
    b = _bubbles(desktop)
    assert b["you"] == ["first question", "second question"], b
    assert b["it"] == ["first answer", "second answer"], b


def test_a_reply_interrupted_by_a_tool_keeps_what_came_before(desktop):
    """voice._replyBuf is per model round-trip and stream_start clears it, so
    a turn that looks something up part-way through gets several. Rendering
    from that buffer put the second round's text on screen INSTEAD of the
    first round's rather than after it."""
    _app(desktop)
    _voice_on(desktop)
    desktop.page.evaluate("() => submitVoiceRequest('what changed?')")
    _voice_ev(desktop, {"type": "stream_start"})
    _voice_ev(desktop, {"type": "content", "text": "Let me look. "})
    _voice_ev(desktop, {"type": "tool_call", "name": "read_file",
                        "args": {"path": "a.py"}})
    _voice_ev(desktop, {"type": "stream_start"})
    _voice_ev(desktop, {"type": "content", "text": "Two files."})
    assert _bubbles(desktop)["it"] == ["Let me look. Two files."]
