"""Push-to-talk, driven through a real microphone-shaped stream.

"Nothing happens when I hold it" -- and it was dead for the rest of the
session, but only under one combination, which is why it looked random.

Hands-free is the default. Its VAD opens a recording when it hears something,
and `vadTick` is the only thing that ever ends one. `vadTick` returns at its
first line once push-to-talk is on. So switching modes while it happened to be
hearing something left `voice.recording` true with nothing left alive to clear
it -- and `pttPress` refuses to start while that flag is set. Switch in a quiet
moment and the button worked; switch while the room had any noise in it, or
while you were mid-word, and it never worked again.

toggleMute and replayLastReply both already ended an in-flight utterance before
changing state. setVoicePtt was the one that didn't.

The microphone here is a real MediaStream off an AudioContext, so the VAD, the
MediaRecorder and the energy maths are all the actual ones -- a stubbed
`voice.recording` would have proved nothing, since the bug IS which code path
gets to clear it.
"""

BASE = {
    "mode": "ask", "vision_route": "auto", "thinking_mode": "medium",
    "voice_engine": "local", "live_available": True, "live_voice": "Puck",
    "tts_engine": "kokoro", "voice_reply_language": "en",
    "voice_ptt_key": "Space", "voice_sensitivity": 1.0, "voice_silence_ms": 750,
}


def _mic(loud):
    """A stream that is either silent or continuously voiced.

    Loud is the interesting one: it holds the VAD in a recording, which is the
    state the mode switch has to clean up after.
    """
    return """(gain) => {
      navigator.mediaDevices.getUserMedia = async () => {
        const ctx = new AudioContext();
        const dst = ctx.createMediaStreamDestination();
        const osc = ctx.createOscillator();
        const g = ctx.createGain();
        g.gain.value = gain;
        osc.connect(g); g.connect(dst); osc.start();
        return dst.stream;
      };
    }""", (1 if loud else 0)


def _open_voice(desktop, loud=False, ptt_first=False):
    desktop.boot(boot={"settings": BASE}, voice_mode={"ok": True, "voice_sid": "s1"})
    p = desktop.page
    script, gain = _mic(loud)
    p.evaluate(script, gain)
    if ptt_first:
        p.evaluate("() => document.getElementById('voice-ptt-toggle').click()")
    p.evaluate("() => document.getElementById('voice-chip').click()")
    # Long enough for the noise-floor calibration to finish and the VAD to have
    # opened a recording on the loud stream.
    p.wait_for_timeout(2500)
    return desktop


def _switch_to_ptt(desktop):
    desktop.page.evaluate("() => document.getElementById('voice-ptt-toggle').click()")
    desktop.page.wait_for_timeout(300)


def _hold(desktop, ms=500):
    """Press and hold the button the way a mouse does, and report whether the
    app registered it."""
    p = desktop.page
    p.hover("#voice-ptt-btn")
    p.mouse.down()
    p.wait_for_timeout(ms)
    held = p.eval_on_selector("#voice-ptt-btn", "e => e.classList.contains('held')")
    p.mouse.up()
    p.wait_for_timeout(200)
    return held


def test_holding_the_button_records(desktop):
    _open_voice(desktop, loud=False, ptt_first=True)
    assert _hold(desktop) is True
    assert desktop.errors == []


def test_it_still_works_after_switching_mode_in_a_quiet_room(desktop):
    """The case that always worked, kept honest."""
    _open_voice(desktop, loud=False)
    _switch_to_ptt(desktop)
    assert _hold(desktop) is True


def test_it_works_after_switching_mode_while_the_room_is_noisy(desktop):
    """The reported bug. Hands-free was mid-recording at the moment of the
    switch, and nothing was left alive to end it."""
    _open_voice(desktop, loud=True)
    _switch_to_ptt(desktop)
    assert _hold(desktop) is True, \
        "the button is dead because a hands-free recording was never closed out"


def test_switching_mode_does_not_leave_a_recording_open(desktop):
    """Stated against the symptom rather than the flag: a second press has to
    work too, so the first one genuinely completed."""
    _open_voice(desktop, loud=True)
    _switch_to_ptt(desktop)
    assert _hold(desktop) is True
    assert _hold(desktop) is True, "the second hold is where a stuck flag shows"


def test_the_button_does_not_come_back_looking_held(desktop):
    """Switching away mid-hold used to leave the held styling on, so
    push-to-talk came back green and already-pressed."""
    _open_voice(desktop, loud=False, ptt_first=True)
    p = desktop.page
    p.hover("#voice-ptt-btn")
    p.mouse.down()
    p.wait_for_timeout(200)
    # Switch to hands-free without releasing.
    p.evaluate("() => document.getElementById('voice-ptt-toggle').click()")
    p.mouse.up()
    p.wait_for_timeout(200)
    _switch_to_ptt(desktop)
    assert p.eval_on_selector("#voice-ptt-btn", "e => e.classList.contains('held')") is False


def test_the_button_is_only_there_in_push_to_talk(desktop):
    """Hands-free has nothing to hold, and a dead button on screen is its own
    kind of "nothing happens"."""
    _open_voice(desktop, loud=False)
    p = desktop.page
    assert p.eval_on_selector("#voice-ptt-btn", "e => e.hidden") is True
    _switch_to_ptt(desktop)
    assert p.eval_on_selector("#voice-ptt-btn", "e => e.hidden") is False


def test_the_screen_says_which_mode_it_is_in(desktop):
    _open_voice(desktop, loud=False)
    p = desktop.page
    _switch_to_ptt(desktop)
    assert "hold" in p.text_content("#voice-status").lower()
    assert p.text_content("#voice-ptt-toggle") == "Push-to-talk"
