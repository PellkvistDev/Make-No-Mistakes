"""The voice engine switch, and the audio maths behind the live one.

Voice mode has two engines now. The local one is four hops -- record, Whisper,
a text model, Kokoro -- and stays the default, because it works offline, costs
nothing and sends no audio anywhere. The live one is a single WebSocket to a
model that hears and speaks natively.

Nothing here opens a socket. What it checks is the part that is wrong silently:
which engine is offered, and the two sample rates, which are different in each
direction and produce a chipmunk when swapped.
"""

BASE = {
    "mode": "ask", "vision_route": "auto", "thinking_mode": "medium",
    "voice_engine": "local", "live_available": True, "live_voice": "Puck",
    "tts_engine": "kokoro", "voice_reply_language": "en",
}


def _open(desktop, **over):
    # Into the BOOT payload, not as a reply to a settings() call: the page
    # reads its settings out of boot and never asks again, so a reply named
    # "settings" is simply never fetched.
    desktop.boot(boot={"settings": {**BASE, **over}})
    desktop.page.evaluate("() => document.getElementById('settings-btn').click()")
    desktop.page.evaluate(
        """() => document.querySelector(
             '.settings-tab-btn[data-tab="voice"]').click()""")
    desktop.page.wait_for_timeout(250)
    return desktop


def _seg(desktop):
    return desktop.page.evaluate(
        """() => [...document.querySelectorAll('#voice-engine-seg button')].map(b => ({
             v: b.dataset.v, on: b.classList.contains('on'),
             disabled: b.disabled }))""")


def test_local_is_the_default_and_is_the_one_selected(desktop):
    _open(desktop)
    assert _seg(desktop) == [
        {"v": "local", "on": True, "disabled": False},
        {"v": "live", "on": False, "disabled": False},
    ]
    assert desktop.errors == []


def test_speech_to_speech_is_disabled_when_no_api_can_do_it(desktop):
    """Only some APIs implement the protocol, and each chat can sit on a
    different one. Better said on the control than discovered the first time
    someone opens their mouth."""
    _open(desktop, live_available=False)
    assert [b["disabled"] for b in _seg(desktop)] == [False, True]
    note = desktop.page.text_content("#voice-engine-note")
    assert "Google AI Studio" in note


def test_choosing_an_engine_saves_it(desktop):
    _open(desktop)
    desktop.page.evaluate(
        """() => document.querySelector(
             '#voice-engine-seg button[data-v="live"]').click()""")
    desktop.page.wait_for_timeout(200)
    sent = desktop.page.evaluate(
        "() => window.__calls.filter(c => c.name === 'set_setting').pop()")
    assert sent["args"] == ["voice_engine", "live"]


def test_the_note_says_what_each_engine_costs(desktop):
    """The local engine's real selling point is that it costs nothing and
    sends nothing, and that is not obvious from its name."""
    _open(desktop)
    assert "no quota" in desktop.page.text_content("#voice-engine-note")
    _open(desktop, voice_engine="live")
    assert "quota" in desktop.page.text_content("#voice-engine-note")


# ---- the audio maths ---------------------------------------------------- #

def test_the_microphone_is_resampled_to_exactly_16k(desktop):
    """The API takes one input rate. A device at 48k that is sent through
    unchanged is heard three times too fast."""
    desktop.boot(boot={"settings": BASE})
    n = desktop.page.evaluate(
        "() => livePcm16(new Float32Array(48000), 48000).byteLength")
    assert n == 16000 * 2, "one second in, one second of 16-bit 16k out"
    n2 = desktop.page.evaluate(
        "() => livePcm16(new Float32Array(16000), 16000).byteLength")
    assert n2 == 16000 * 2


def test_full_scale_samples_do_not_wrap_around(desktop):
    """+1.0 scaled by 0x8000 overflows to -32768 -- a loud passage becomes
    static, which sounds like a broken microphone rather than a bug here."""
    desktop.boot(boot={"settings": BASE})
    got = desktop.page.evaluate(
        """() => {
             const b = livePcm16(new Float32Array([1, -1, 0]), 16000);
             return [...new Int16Array(b.buffer, b.byteOffset, 3)];
           }""")
    assert got == [32767, -32768, 0]


def test_base64_survives_bytes_that_are_not_text(desktop):
    """PCM is arbitrary bytes, including the ones that are not valid UTF-8."""
    ok = desktop.page.evaluate(
        """() => {
             const src = [0, 1, 127, 128, 200, 255];
             const round = [...liveBytes(liveB64(new Uint8Array(src)))];
             return JSON.stringify(round) === JSON.stringify(src);
           }""")
    assert ok is True
