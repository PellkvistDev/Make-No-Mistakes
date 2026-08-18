"""The live engine, driven through the real app against a stub socket.

Four things were wrong once the engine toggle said "Gemini Live", and they
shared a cause: the live path was bolted alongside the local one rather than
into it, so everything the local engine owned -- the push-to-talk state, the
"is it speaking" flag, the on-screen transcript -- was simply absent here.

  1. Push-to-talk did nothing to the live stream. The microphone was sent
     continuously no matter what the toggle said, and the BUTTON drove the
     local recorder, so choosing live + push-to-talk gave you a hands-free
     live session with a local transcribe-and-send running on top of it.
  2. Finished workers talked over the model, and over each other. The queue's
     guard tested voice.speaking, which the live engine never sets.
  3. Worker reports reached the delegator and nothing else, so the coding
     agent could not be asked about work dispatched by voice.
  4. The transcript was overwritten with a single line of plain text, which is
     why the desktop appeared to have none in live mode while the phone did.

Nothing here opens a real socket: the point is what the app SENDS and when,
which a stub records exactly and a real connection would only obscure.
"""

BASE = {
    "mode": "ask", "vision_route": "auto", "thinking_mode": "medium",
    "voice_engine": "live", "live_available": True, "live_voice": "Puck",
    "tts_engine": "kokoro", "voice_reply_language": "en",
    "voice_ptt_key": "Space", "voice_sensitivity": 1.0, "voice_silence_ms": 750,
}

# A microphone, and a WebSocket that records instead of connecting.
STUBS = r"""() => {
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new AudioContext();
    const dst = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    g.gain.value = 1;
    osc.connect(g); g.connect(dst); osc.start();
    return dst.stream;
  };
  window.__sent = [];
  class FakeWS {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      window.__ws = this;
      setTimeout(() => { if (this.onopen) this.onopen(); }, 0);
    }
    send(s) { window.__sent.push(JSON.parse(s)); }
    close() { this.readyState = 3; }
  }
  FakeWS.OPEN = 1;
  window.WebSocket = FakeWS;
  // Deliver a server frame the way the socket would.
  window.__serverSays = (obj) => window.__ws.onmessage({ data: JSON.stringify(obj) });
}"""


def _open(desktop, ptt=False):
    desktop.boot(boot={"settings": BASE}, voice_mode={"ok": True, "voice_sid": "s1"})
    p = desktop.page
    p.evaluate(STUBS)
    p.evaluate("""() => window.__reply('live_voice_config', {
                    url: 'wss://example.invalid/live',
                    setup: { setup: { model: 'm' } } })""")
    if ptt:
        p.evaluate("() => document.getElementById('voice-ptt-toggle').click()")
    p.evaluate("() => document.getElementById('voice-chip').click()")
    p.wait_for_timeout(700)
    p.evaluate("() => window.__serverSays({ setupComplete: {} })")
    p.wait_for_timeout(150)
    return p


def _audio_frames(p):
    return p.evaluate(
        "() => window.__sent.filter(m => m.realtimeInput && m.realtimeInput.audio).length")


def _texts(p):
    return p.evaluate(
        """() => window.__sent.filter(m => m.realtimeInput && m.realtimeInput.text)
                              .map(m => m.realtimeInput.text)""")


def _stream_ends(p):
    return p.evaluate(
        """() => window.__sent.filter(
             m => m.realtimeInput && m.realtimeInput.audioStreamEnd).length""")


# ------------------------------------------------------- 1. push-to-talk --

def test_hands_free_streams_the_microphone(desktop):
    """The control: with the toggle off, audio flows on its own."""
    p = _open(desktop, ptt=False)
    p.wait_for_timeout(600)
    assert _audio_frames(p) > 0
    assert desktop.errors == []


def test_push_to_talk_sends_nothing_until_the_button_is_held(desktop):
    """The bug. This engine ignored voice.ptt entirely, so picking
    push-to-talk still streamed the room to the model continuously."""
    p = _open(desktop, ptt=True)
    p.wait_for_timeout(700)
    assert _audio_frames(p) == 0, "push-to-talk must not stream while idle"

    p.evaluate("() => document.getElementById('voice-ptt-btn').dispatchEvent("
               "new MouseEvent('mousedown'))")
    p.wait_for_timeout(700)
    assert _audio_frames(p) > 0, "holding the button must open the stream"
    assert desktop.errors == []


def test_releasing_stops_the_stream_and_flushes_the_turn(desktop):
    p = _open(desktop, ptt=True)
    btn = "document.getElementById('voice-ptt-btn')"
    p.evaluate(f"() => {btn}.dispatchEvent(new MouseEvent('mousedown'))")
    p.wait_for_timeout(600)
    p.evaluate(f"() => {btn}.dispatchEvent(new MouseEvent('mouseup'))")
    sent_at_release = _audio_frames(p)

    # audioStreamEnd, or the server waits on its own endpointer for silence it
    # is no longer being sent.
    assert _stream_ends(p) >= 1

    p.wait_for_timeout(600)
    assert _audio_frames(p) == sent_at_release, "released, but still streaming"
    assert desktop.errors == []


def test_the_local_recorder_is_never_started_in_live_mode(desktop):
    """What "it's still using the local stuff" meant: the button ran
    startUtterance, so the clip went to local transcription and came back as
    typed text -- in a session whose whole point is that the model hears you."""
    p = _open(desktop, ptt=True)
    p.evaluate("() => document.getElementById('voice-ptt-btn').dispatchEvent("
               "new MouseEvent('mousedown'))")
    p.wait_for_timeout(500)
    assert p.evaluate("() => window.voice ? window.voice.recording : false") is False
    p.evaluate("() => document.getElementById('voice-ptt-btn').dispatchEvent("
               "new MouseEvent('mouseup'))")
    p.wait_for_timeout(400)
    called = p.evaluate("() => window.__calls.map(c => c.name)")
    assert "transcribe_audio" not in called
    assert "send_voice" not in called


# ------------------------------------------- 2. one voice at a time -------

def _finish_worker(p, name, result="all done"):
    p.evaluate(
        """(a) => window.GLM.emit({ type: 'worker_update', sid: 's1::voice',
              id: a.name, name: a.name, status: 'done', result: a.result })""",
        {"name": name, "result": result})


def test_a_finished_worker_is_announced_through_the_live_session(desktop):
    """Not through the local text-to-speech. Announcing locally is what put two
    different voices in the room at the same time, one of them reading from a
    conversation the live model had never seen."""
    p = _open(desktop)
    _finish_worker(p, "tidy", "Rewrote the README")
    p.wait_for_timeout(400)

    said = _texts(p)
    assert any("tidy" in t and "Rewrote the README" in t for t in said)
    names = p.evaluate("() => window.__calls.map(c => c.name)")
    assert "announce_worker" not in names, "the local voice must stay out of this"
    assert "record_worker_result" in names, "the report still has to be filed"


def test_nothing_is_announced_while_the_model_is_talking(desktop):
    """voice.speaking is a local-engine flag and is never set here, so every
    guard in the queue passed while Gemini was mid-sentence."""
    p = _open(desktop)
    # Two seconds of audio scheduled: the model is busy talking.
    p.evaluate("""() => {
      const pcm = new Int16Array(48000);
      let s = '';
      const b = new Uint8Array(pcm.buffer);
      for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      window.__serverSays({ serverContent: { modelTurn: { parts: [
        { inlineData: { mimeType: 'audio/pcm', data: btoa(s) } }] } } });
    }""")
    p.wait_for_timeout(200)
    # Observed through the UI rather than through internals: the orb is the
    # app's own statement that the model is talking.
    assert p.evaluate(
        "() => document.getElementById('voice-orb').className"
    ).endswith("speaking")

    _finish_worker(p, "slow")
    p.wait_for_timeout(400)
    assert _texts(p) == [], "it spoke over the model"
    assert desktop.errors == []


def test_two_workers_finishing_together_speak_one_at_a_time(desktop):
    p = _open(desktop)
    _finish_worker(p, "one", "first result")
    _finish_worker(p, "two", "second result")
    p.wait_for_timeout(500)

    said = _texts(p)
    assert len(said) == 1, f"both announced at once: {said}"
    assert "one" in said[0]


# ------------------------------------------------- 4. the transcript ------

def _transcript(p):
    return p.evaluate(
        """() => [...document.querySelectorAll('#voice-caption .voice-turn')].map(b => ({
             you: (b.querySelector('.voice-you') || {}).textContent || '',
             it: (b.querySelector('.voice-it') || {}).textContent || '' }))""")


def test_both_halves_of_a_spoken_turn_are_written_down(desktop):
    p = _open(desktop)
    p.evaluate("""() => {
      window.__serverSays({ serverContent: {
        inputTranscription: { text: 'add a dark mode' } } });
      window.__serverSays({ serverContent: {
        outputTranscription: { text: 'Starting on that now.' } } });
      window.__serverSays({ serverContent: { turnComplete: true } });
    }""")
    p.wait_for_timeout(250)

    assert _transcript(p) == [{"you": "add a dark mode", "it": "Starting on that now."}]
    assert desktop.errors == []


def test_the_transcript_accumulates_instead_of_being_replaced(desktop):
    """It used to assign textContent on the whole caption element, so each
    turn wiped every turn before it -- and wiped the local engine's blocks too,
    which is what made the desktop look like it had no transcript at all."""
    p = _open(desktop)
    for heard, said in (("first question", "first answer"),
                        ("second question", "second answer")):
        p.evaluate("""(t) => {
          window.__serverSays({ serverContent: { inputTranscription: { text: t.heard } } });
          window.__serverSays({ serverContent: { outputTranscription: { text: t.said } } });
          window.__serverSays({ serverContent: { turnComplete: true } });
        }""", {"heard": heard, "said": said})
        p.wait_for_timeout(150)

    rows = _transcript(p)
    assert len(rows) == 2, f"expected both turns, got {rows}"
    assert rows[0]["you"] == "first question"
    assert rows[1]["it"] == "second answer"


def test_streamed_partials_update_one_block_rather_than_adding_many(desktop):
    """Transcriptions arrive in fragments; a block per fragment would be a
    column of single words."""
    p = _open(desktop)
    p.evaluate("""() => {
      window.__serverSays({ serverContent: { inputTranscription: { text: 'add ' } } });
      window.__serverSays({ serverContent: { inputTranscription: { text: 'dark ' } } });
      window.__serverSays({ serverContent: { inputTranscription: { text: 'mode' } } });
    }""")
    p.wait_for_timeout(200)

    rows = _transcript(p)
    assert len(rows) == 1
    assert rows[0]["you"] == "add dark mode"


# ------------------------------------- 5. it reaches the text chat too ----
#
# Reported after the transcript landed in the overlay: it is still only IN the
# overlay. Close it and the chat window shows whatever had been typed before,
# as if the conversation had not happened -- and the coding agent, asked about
# it by typing, had never heard any of it. The phone has always put spoken
# turns straight into the conversation (voiceRecordTurn).

# The event carries the CODING chat's sid in the app (WebEvents.emit stamps
# it), and handle() routes a sid that is not the active chat to the sidebar
# instead of the transcript -- correctly: a background chat must not draw into
# the window you are looking at. This harness never opens a chat, so
# activeSessionId is null and every sid would take that branch. The events here
# are therefore emitted without one, which exercises the handler itself; that
# it is emitted on the coding sink rather than the overlay's is pinned in
# tests/test_voice_in_the_chat.py, where both sinks exist.

def _chat_messages(p):
    return p.evaluate(
        """() => [...document.querySelectorAll('#chat .msg')].map(m => ({
             who: m.className.includes('msg-user') ? 'user' : 'assistant',
             text: (m.querySelector('.md') || m).textContent.trim() }))""")


def test_a_spoken_exchange_appears_in_the_text_chat(desktop):
    p = _open(desktop)
    p.evaluate("""() => window.GLM.emit({ type: 'voice_chat_turn',
                    user: 'rename the click handler',
                    assistant: 'Renamed it in two files.' })""")
    p.wait_for_timeout(200)

    rows = _chat_messages(p)
    assert any(r["who"] == "user" and "rename the click handler" in r["text"]
               for r in rows), f"the spoken question is missing: {rows}"
    assert any(r["who"] == "assistant" and "Renamed it in two files." in r["text"]
               for r in rows), f"the spoken answer is missing: {rows}"
    assert desktop.errors == []


def test_spoken_turns_accumulate_alongside_typed_ones(desktop):
    """The point of the whole change: talking and typing are one conversation,
    so they interleave rather than living in two places."""
    p = _open(desktop)
    for said in ("first spoken thing", "second spoken thing"):
        p.evaluate("""(t) => window.GLM.emit({ type: 'voice_chat_turn',
                        user: t, assistant: 'ok' })""", said)
        p.wait_for_timeout(120)

    text = " ".join(r["text"] for r in _chat_messages(p))
    assert "first spoken thing" in text and "second spoken thing" in text


def test_an_answer_with_no_question_still_renders(desktop):
    """A worker announcement is spoken without anything being asked."""
    p = _open(desktop)
    p.evaluate("""() => window.GLM.emit({ type: 'voice_chat_turn',
                    user: '', assistant: 'The tidy worker finished.' })""")
    p.wait_for_timeout(200)

    rows = _chat_messages(p)
    assert any("tidy worker finished" in r["text"] for r in rows)
    assert desktop.errors == []
