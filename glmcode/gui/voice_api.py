"""Everything the desktop does with speech: hearing, talking, and the spoken
conversation that sits alongside the typed one.

The second seam cut out of gui/app.py, after DeviceApi. Same rule: a subject
with its own vocabulary, not a line count. The vocabulary here is the
delegator agent (`cs.convo_agent`), its own event sink on the `<sid>::voice`
id, the convo lock, the two queues that carry spoken work back to the coding
agent -- and, at the bottom of the stack, the local TTS and STT engines that
the settings panel probes and the overlay drives.

Both halves are one subject because the app has two speech engines and the
choice is per-session. Gemini Live hears and speaks for itself; the local
engine is Whisper plus Kokoro/Piper with this app in between. `voice_mode`
readies one, `live_voice_config` readies the other, and `_persist_voice_turn`
records the result identically whichever one ran -- so splitting "the spoken
conversation" from "the speech engines" would cut through the middle of that
rather than along a seam.

A MIXIN, for the reason DeviceApi is one: pywebview exposes the Api
instance's public methods by inspection, so an inherited method is found
exactly like a defined one, while a method moved onto a collaborator object
would not be -- and would fail only at runtime, in the app, on the one path
nobody re-tests. These methods also reach all over the instance (`self._cfg`,
`self._active`, `self._events`, `self._save_chat`, `self._os_attention`,
`self._window`, `self._perm_registry`), which a collaborator would have to be
handed anyway.

The proof this preserved behaviour is that the voice tests were not modified.
"""

from __future__ import annotations

import base64
import threading
import uuid

from ..agent import Agent
from ..config import (CONFIG_DIR, find_provider, default_provider,
                      provider_key as cfg_provider_key)
from ..errors import ToolError
from .. import live
from ..prompts import worker_report_note
from ..tools import CONVERSATIONAL_SCHEMAS
from .events import WebEvents
from .media import _data_uri
from .speech import _tts_engine_voice


class VoiceApi:
    """Speech-to-text, text-to-speech, and speech-to-speech. Mixed into Api."""

    # -- text-to-speech -------------------------------------------------------- #

    def tts_status(self):
        from .. import tts_engine
        engine, voice = _tts_engine_voice(self._cfg)
        return {"ready": tts_engine.ready(engine, voice)}

    def tts_voices(self, engine: str = ""):
        from .. import tts_engine
        engine = engine or (self._cfg.tts_engine if self._cfg else "kokoro") or "kokoro"
        return {"voices": tts_engine.list_voices(engine), "engine": engine,
                "default": tts_engine.default_voice(engine)}

    def stt_status(self, model: str = ""):
        """Whether dictation is ready to go for the given model (packages
        installed AND that model already downloaded). Settings uses this to
        show/hide the one-time-download note."""
        from .. import stt as stt_mod
        return {"ready": stt_mod.ready(model or stt_mod.DEFAULT_MODEL)}

    def preview_voice(self, voice: str, engine: str = ""):
        """Synthesize (once, then cached on disk) and return a short sample
        of the given voice so Settings can offer an audition button. Can
        take a while on the very first call ever (full first-use install +
        download), same as any other first TTS use."""
        from .. import tts_engine
        engine = engine or (self._cfg.tts_engine if self._cfg else "kokoro") or "kokoro"
        voice = (voice or "").strip() or tts_engine.default_voice(engine)
        cache_dir = CONFIG_DIR / "models" / ("piper" if engine == "piper" else "kokoro") / "previews"
        cache_path = cache_dir / f"{voice}.wav"
        if not cache_path.is_file():
            try:
                tts_engine.save_wav(f"Hi, this is the {voice} voice.", cache_path,
                                    voice=voice, engine=engine, status=self._events.info)
            except Exception as e:
                return {"error": str(e)}
        try:
            return {"ok": True, "src": _data_uri(cache_path)}
        except OSError as e:
            return {"error": str(e)}

    # -- speech-to-text (voice input) -------------------------------------- #

    def transcribe_audio(self, data_url: str):
        """Transcribe a recorded audio clip (a base64 data URL captured by the
        composer's mic button) to text, locally via faster-whisper. Returns
        {"text": ...}; the FIRST call installs faster-whisper + downloads the
        model (~50MB+), same one-time cost as the other local models."""
        from .. import stt as stt_mod
        try:
            head, _, b64 = str(data_url or "").partition(",")
            if not b64 or not head.startswith("data:audio"):
                return {"error": "No audio was captured."}
            ext = ".webm" if "webm" in head else (".ogg" if "ogg" in head else ".wav")
            raw = base64.b64decode(b64)
            if len(raw) < 512:
                return {"text": ""}   # basically silence / an empty clip
        except Exception:
            return {"error": "Could not read the recorded audio."}
        folder = CONFIG_DIR / "stt-tmp"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (uuid.uuid4().hex + ext)
        def _status(msg):
            # The routine per-clip "Transcribing…" is already shown in the UI
            # (mic button / voice overlay), so don't save it into the chat.
            # Only the one-time install/download is worth surfacing -- and as a
            # transient toast, never a saved notice.
            m = str(msg or "")
            if "Transcrib" in m:
                return
            self._events.toast(m, "info")

        try:
            path.write_bytes(raw)
            text = stt_mod.transcribe(
                path, model=(self._cfg.stt_model or stt_mod.DEFAULT_MODEL),
                language=self._cfg.stt_language, status=_status)
            return {"text": text}
        except Exception as e:
            return {"error": f"Transcription failed: {e}"}
        finally:
            try:
                path.unlink()
            except OSError:
                pass


    # -- speech-to-speech voice mode --------------------------------------- #

    def _voice_sid(self, sid: str) -> str:
        return f"{sid}::voice"

    def _ensure_convo(self, cs: "ChatState") -> "Agent":
        """The chat's conversational (delegator) agent, built on first use. It
        shares the coding chat's project, backups, MCP and model so the workers
        it dispatches operate on the real code -- but keeps its own short spoken
        conversation and its own event stream (the voice overlay)."""
        if cs.convo_agent is not None:
            return cs.convo_agent
        coding = cs.agent
        vsid = self._voice_sid(cs.sid)
        ev = WebEvents(vsid, self._perm_registry)
        ev._cfg = self._cfg
        ev._window = self._window
        ev.notifier = lambda body, _sid=cs.sid: self._os_attention(_sid, body)
        convo = Agent(self._cfg, coding.client, events=ev,
                      workdir=coding.workdir, conversational=True)
        # Share the things a dispatched worker needs to act on the real project.
        convo.backup_repo = coding.backup_repo
        convo.mcp = coding.mcp
        convo.model_override = coding.model_override
        convo.vision_client = coding.vision_client
        cs.convo_agent = convo
        cs.convo_events = ev
        return convo

    def voice_mode(self, on: bool):
        """Turn speech-to-speech mode on/off for the active chat. Turning it on
        readies the conversational agent and pre-warms the local speech models
        (so the first utterance isn't stuck behind a cold load); the audio loop
        lives in the UI."""
        cs = self._active
        if cs is None:
            return {"error": "no active chat — start a New Chat first"}
        if on:
            self._ensure_convo(cs)
            self._prewarm_speech()
            return {"ok": True, "voice_sid": self._voice_sid(cs.sid)}
        # Turning voice off: release any workers blocked waiting for a spoken OK
        # (their approve/deny card is going away), so they don't hang.
        if cs.convo_agent is not None:
            cs.convo_agent.deny_pending_worker_permissions("voice mode was closed")
        return {"ok": True}

    # -- speech to speech (Gemini Live) ---------------------------------- #

    def live_voice_config(self):
        """Everything the page needs to open a live session, or why it can't.

        The socket is opened by the page and not here on purpose: the mic and
        the speakers are already there, and pywebview's bridge is the app's
        narrowest pipe -- WebEvents batches its traffic for exactly that
        reason. Streaming 16kHz PCM through it in both directions would be the
        worst thing this app could ask of it.

        So what crosses the bridge is this: once, at the start, the address and
        the shape of the session. The audio never does.
        """
        cs = self._active
        if cs is None:
            return {"error": "no active chat — start a New Chat first"}
        prov = find_provider(self._cfg, self.session_provider) \
            or default_provider(self._cfg)
        if prov is None:
            return {"error": "no API configured"}
        model = live.available(prov["base_url"])
        if not model:
            # Named, rather than "unsupported". Which API a chat is on is a
            # per-chat choice now, so the fix is a specific one.
            return {"error": f"{prov['name']} has no speech-to-speech model. "
                             "Switch this chat to Google AI Studio for voice, "
                             "or use the local voice engine in Settings."}
        key = cfg_provider_key(prov)
        if not key:
            return {"error": f"no API key for {prov['name']}"}
        convo = self._ensure_convo(cs)
        lang = "" if self._cfg.voice_reply_language == "match" else "en-US"
        return {
            "ok": True,
            "url": live.ws_url(key),
            "setup": live.setup_message(
                model, convo.system_prompt_text(), CONVERSATIONAL_SCHEMAS,
                voice=self._cfg.live_voice, language=lang),
            "model": model,
            "inputRate": live.INPUT_SAMPLE_RATE,
            "outputRate": live.OUTPUT_SAMPLE_RATE,
            "voice_sid": self._voice_sid(cs.sid),
        }

    def live_voice_tool(self, name: str, args: dict):
        """Run one tool the live model asked for, and report what happened.

        Synchronous, because the Live API's function calling is: the model is
        stopped mid-conversation until this returns. That would be a bad trade
        for a voice assistant, except that these particular tools are already
        built to return instantly -- dispatch_worker exists so the assistant
        never goes quiet while real work happens, which is the same property
        this protocol requires. The constraint and the design agree.

        Never raises. A tool that fails is a thing the model should hear about
        and talk about, not a dropped socket.
        """
        cs = self._active
        if cs is None or cs.convo_agent is None:
            return {"output": "ERROR: this chat is no longer open."}
        try:
            return {"output": cs.convo_agent._run_tool(name, dict(args or {}))}
        except ToolError as e:
            return {"output": f"ERROR: {e}"}
        except Exception as e:
            return {"output": f"ERROR: unexpected {type(e).__name__}: {e}"}

    def live_voice_turn(self, user_text: str, reply_text: str):
        """Record a completed spoken exchange.

        The model keeps the conversation on its side, so nothing about a live
        session lands in this app by itself. Both halves come back as
        transcriptions and are written to the chat's searchable transcript --
        the same log the local engine writes, so a coding chat can read what
        was said out loud whichever engine said it.
        """
        cs = self._active
        if cs is None:
            return {"ok": False}
        convo = cs.convo_agent
        if convo is not None:
            # Kept in the agent's own history too, so switching back to the
            # local engine mid-conversation continues rather than restarts.
            if user_text:
                convo.messages.append({"role": "user", "content": user_text})
            if reply_text:
                convo.messages.append({"role": "assistant", "content": reply_text})
        self._persist_voice_turn(cs, user_text or "", reply=reply_text or "")
        return {"ok": True}

    def _prewarm_speech(self) -> None:
        """Load the STT + TTS models in the background so voice mode's first
        turn is fast. Best-effort and non-blocking; each only warms if already
        installed (never triggers a surprise first-use download)."""
        model = self._cfg.stt_model or "base"

        def warm():
            try:
                from .. import stt as stt_mod
                stt_mod.prewarm(model)
            except Exception:
                pass
            try:
                from .. import tts_engine
                engine, voice = _tts_engine_voice(self._cfg)
                tts_engine.prewarm(engine, voice)
            except Exception:
                pass
            try:
                self._ack_audio(self._ACK_PHRASES[0])  # cache one, so the first "Yes?" is instant
            except Exception:
                pass
        threading.Thread(target=warm, daemon=True).start()

    def cancel_voice(self):
        """Interrupt the conversational agent's current reply (barge-in): stop
        it generating and drop any queued worker announcements, so when the user
        cuts in it actually stops instead of talking over them."""
        cs = self._active
        if cs is None or cs.convo_agent is None:
            return {"ok": True}
        try:
            cs.convo_agent.request_cancel()
        except Exception:
            pass
        return {"ok": True}

    _ACK_PHRASES = ("Mm-hm?", "Yes?", "Go ahead.", "I'm listening.", "Yeah?")

    def voice_ack(self):
        """Speak a short acknowledgement ("Yes?") when the wake word opens the
        mic, so the user hears that it's listening. Synthesized off-thread and
        played via a dedicated voice_ack event; cached per engine/voice/phrase
        so it's instant after the first time."""
        cs = self._active
        if cs is None or cs.convo_events is None:
            return {"ok": False}
        import random
        phrase = random.choice(self._ACK_PHRASES)
        ev = cs.convo_events

        def make():
            try:
                src = self._ack_audio(phrase)
            except Exception:
                src = ""
            if src:
                ev.emit("voice_ack", src=src)
        threading.Thread(target=make, daemon=True).start()
        return {"ok": True}

    def _ack_audio(self, phrase: str) -> str:
        engine, voice = _tts_engine_voice(self._cfg)
        key = (engine, voice, phrase)
        cache = getattr(self, "_ack_cache", None)
        if cache is None:
            cache = self._ack_cache = {}
        if key in cache:
            return cache[key]
        from .. import tts_engine
        speed = (self._cfg.tts_speed if self._cfg else None) or 1.0
        audio, sr = tts_engine.synthesize(phrase, voice=voice, speed=speed, engine=engine)
        src = "data:audio/wav;base64," + base64.b64encode(
            tts_engine.audio_to_wav_bytes(audio, sr)).decode("ascii")
        cache[key] = src
        return src

    def resolve_worker_permission(self, rid: str, answer: str, feedback: str = ""):
        """Answer a background worker's permission request (approve-by-voice or
        the overlay buttons). answer: 'y' (once), 'a' (always this kind), 'n'."""
        cs = self._active
        if cs is None or cs.convo_agent is None:
            return {"ok": False}
        ans = answer if answer in ("y", "a", "n") else "n"
        ok = cs.convo_agent.resolve_worker_permission(rid, ans, feedback)
        return {"ok": bool(ok)}

    def send_voice(self, text: str):
        """One spoken user turn to the conversational agent. Runs on its own
        thread and streams through the voice events; replies are always read
        aloud (it's a voice conversation). Returns busy if it's mid-reply."""
        cs = self._active
        if cs is None:
            return {"error": "no active chat"}
        text = (text or "").strip()
        if not text:
            return {"error": "empty"}
        if not cs.convo_lock.acquire(blocking=False):
            return {"error": "busy"}
        self._ensure_convo(cs)
        threading.Thread(target=self._run_convo_turn,
                         args=(cs, {"role": "user", "content": text}, text),
                         daemon=True).start()
        return {"ok": True, "started": True}

    def announce_worker(self, name: str, status: str, result: str):
        """Have the conversational agent tell the user out loud that a background
        worker finished. Called by the UI when a worker_update lands. Runs a
        short convo turn from a system-style note; skipped if it's mid-reply so
        the UI should re-try (it queues these)."""
        cs = self._active
        if cs is None or cs.convo_agent is None:
            return {"error": "no voice session"}
        if not cs.convo_lock.acquire(blocking=False):
            return {"error": "busy"}
        outcome = "finished successfully" if status == "done" else "failed"
        result = str(result or "")[:2000]
        note = (f"[System note — not from the user] The background worker "
                f"'{name}' just {outcome}. Its result:\n{result}\n\n"
                f"Briefly tell the user out loud what happened, in plain "
                f"spoken language. Do not read code or paths aloud.")
        # Also filed for the coding agent, so the report survives the voice
        # session: announce_worker only ever reached the delegator.
        self._queue_worker_report(cs, worker_report_note(name, status, result))
        threading.Thread(target=self._run_convo_turn,
                         args=(cs, {"role": "user", "content": note}),
                         daemon=True).start()
        return {"ok": True, "started": True}

    def record_worker_result(self, name: str, status: str, result: str):
        """File a finished worker's report without speaking it.

        The live engine says these itself (the announcement is sent into its
        own session), so the local announce path would be a second voice. But
        the RESULT still has to be recorded, and in two places:

          - the delegator's history, so a follow-up question in the same
            spoken conversation has the report to answer from;
          - the coding agent's queue, so switching back to typing can ask
            about work that was dispatched by voice. Without this the report
            existed only as speech: the model that did the work is gone, and
            the model you are typing to never heard about it.
        """
        cs = self._active
        if cs is None:
            return {"ok": False}
        note = worker_report_note(name, status, result)
        convo = cs.convo_agent
        if convo is not None:
            convo.messages.append({"role": "user", "content": note})
        self._queue_worker_report(cs, note)
        return {"ok": True}

    def _queue_worker_report(self, cs: "ChatState", note: str) -> None:
        with cs.worker_reports_lock:
            # Bounded: a long voice session can dispatch a lot of workers, and
            # every one of these is spent from the coding agent's context the
            # moment it next runs.
            cs.worker_reports.append(note)
            del cs.worker_reports[:-20]

    def _drain_worker_reports(self, cs: "ChatState") -> None:
        """Hand queued worker reports to the coding agent. Called with the turn
        lock held, so appending to its history is safe here and nowhere else."""
        with cs.worker_reports_lock:
            notes, cs.worker_reports = cs.worker_reports, []
        for note in notes:
            try:
                cs.agent.messages.append({"role": "user", "content": note})
            except Exception:
                pass

    def _run_convo_turn(self, cs: "ChatState", msg: dict,
                        user_text: str = "") -> None:
        """Body of one voice turn, on its own thread. Mirrors _run_send_turn but
        for the delegator agent: no @-mentions, no backups, no titling -- just
        talk. The convo_lock (acquired by the caller) is released here.

        `user_text` is the user's spoken words for a real turn (logged to the
        chat's searchable transcript so the voice conversation persists); empty
        for internal turns (worker announcements), which aren't logged as user
        input."""
        convo, ev = cs.convo_agent, cs.convo_events
        ok = False
        try:
            ev.start_turn(True)  # voice replies are always spoken
            convo.run_turn(msg)
            ok = True
        except Exception as e:
            ev.error(f"{type(e).__name__}: {e}")
        finally:
            cs.convo_lock.release()
            self._persist_voice_turn(cs, user_text)
            ev.emit("voice_turn_complete", ok=ok)

    def _persist_voice_turn(self, cs: "ChatState", user_text: str,
                            reply: str | None = None) -> None:
        """Record a completed spoken exchange in the two places it belongs.

        The append-only transcript, which survives compaction and can be
        grepped later -- and, since this is a conversation and not a log entry,
        the CHAT itself. It used to go only to the transcript, so a spoken
        exchange left no trace in the chat you were looking at: you could talk
        for ten minutes, close the overlay, and the window still showed
        whatever had been typed before. The phone has always put spoken turns
        straight into the conversation (voiceRecordTurn), and switching between
        talking and typing should not be a change of subject.
        """
        # The live engine already HAS both halves -- Gemini transcribes the
        # speech in each direction and hands them over -- so it passes them in
        # rather than having them dug back out of the delegator's history,
        # which is a copy and can be absent entirely.
        if reply is None:
            try:
                reply = self._last_convo_reply(cs)
            except Exception:
                reply = ""
        reply = reply or ""
        tr = getattr(cs.agent, "transcript", None)
        if tr is not None:
            try:
                if user_text:
                    tr.user(user_text, label="Voice")
                if reply:
                    tr.assistant(reply)
            except Exception:
                pass
        if user_text or reply:
            self._record_voice_exchange(cs, user_text, reply)

    def _record_voice_exchange(self, cs: "ChatState", user_text: str,
                               reply: str) -> None:
        """Put a spoken exchange into the coding chat: on screen now, and in
        the model's history the moment that is safe.

        Appending straight to agent.messages is only safe while no turn is
        running -- doing it underneath one is how you get a tool_call with no
        matching reply. So the lock is TRIED, and a busy chat queues instead;
        _drain_voice_turns picks it up at the top of the next turn.
        """
        pair = {"user": user_text, "assistant": reply}
        if cs.turn_lock.acquire(blocking=False):
            try:
                self._append_voice_messages(cs, pair)
                self._save_chat(cs)
            finally:
                cs.turn_lock.release()
        else:
            with cs.voice_turns_lock:
                cs.voice_turns.append(pair)
                del cs.voice_turns[:-40]
        # On screen either way, and on the CODING chat's sid -- the voice sid
        # drives the overlay, which is not where this belongs.
        try:
            cs.events.emit("voice_chat_turn", user=user_text, assistant=reply)
        except Exception:
            pass

    @staticmethod
    def _append_voice_messages(cs: "ChatState", pair: dict) -> None:
        if pair.get("user"):
            cs.agent.messages.append({"role": "user", "content": pair["user"]})
        if pair.get("assistant"):
            cs.agent.messages.append(
                {"role": "assistant", "content": pair["assistant"]})

    def _drain_voice_turns(self, cs: "ChatState") -> None:
        """Called with the turn lock held, like _drain_worker_reports."""
        with cs.voice_turns_lock:
            pending, cs.voice_turns = cs.voice_turns, []
        for pair in pending:
            try:
                self._append_voice_messages(cs, pair)
            except Exception:
                pass

    @staticmethod
    def _last_convo_reply(cs: "ChatState") -> str:
        for m in reversed(cs.convo_agent.messages):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) \
                    and m["content"].strip():
                return m["content"].strip()
        return ""
