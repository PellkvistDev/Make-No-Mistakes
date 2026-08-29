"""The agent's event sink for the desktop app: agent events -> JavaScript.

Split out of gui/app.py, which was named after the Api class and had grown to
hold this too. WebEvents is not part of that class and never was -- it is the
other side of the bridge: the agent pushes events in, and they come out as
`window.mnm.on(...)` calls in the page. One instance per chat, each tagged
with its session id so the page can route them.

It is also what the voice seam needed. `_ensure_convo` builds a second sink
for the spoken conversation, and a mixin in its own module cannot construct a
class defined in app.py without an import cycle. Moving the class is the
honest fix; a lazy `from .app import WebEvents` inside the function would work
and would be the seam starting to leak back.

The tests for this class were not touched -- `from glmcode.gui.app import
WebEvents` still resolves, because app.py re-exports it -- which is the proof
that the move changed where the code lives and nothing about what it does.
"""

from __future__ import annotations

import base64
import json
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import webview

from ..events import AgentEvents
from .media import _data_uri
from .speech import _tts_engine_voice


class _TtsFeeder:
    """Accumulates one logical stream of prose (the main agent's own replies,
    or whichever sub-agent's panel is currently focused) and hands back
    complete, speakable chunks once a sentence boundary is reached, so a
    fast-streaming source doesn't fire one tiny synthesis call per token.
    Two unrelated streams must never share a feeder -- their sentences would
    interleave into garbled prose -- which is why WebEvents keeps a separate
    instance per source instead of one shared buffer."""

    _SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?=\s|$)")
    # The very first chunk of a stream uses a much lower min_len than later
    # ones -- e.g. a short opening line like "Sure!" or "Fixed." would
    # otherwise sit in the buffer waiting for a second sentence to reach the
    # normal 40-char floor before any audio starts at all. It can't drop to
    # zero, though: a response that happens to *start* with a short
    # abbreviation ("Mr. Smith says...", "vs. the old approach...") would
    # then get flushed as its own broken one-word utterance the moment the
    # abbreviation's period streams in, before the rest of the sentence
    # arrives. 15 clears virtually all common title/Latin abbreviations
    # (Mr., Dr., vs., etc., i.e., e.g., approx., Corp.) while still cutting
    # latency well below the normal floor for anything longer. Later chunks
    # keep the higher floor: a full-length sentence sounds more natural and
    # is more efficient per synthesis call than many very short ones.
    _FIRST_CHUNK_MIN_LEN = 15

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._raw = ""      # cumulative raw text this stream segment (fence tracking)
        self._sent_len = 0  # how much of the fence-filtered prose is already buffered
        self._buffer = ""   # buffered prose not yet synthesized
        self._first_chunk_done = False

    def feed(self, text: str) -> list[str]:
        """New raw text arrived; returns zero or more complete chunks ready
        to synthesize, in order."""
        from ..tts import strip_code_fences_incremental
        self._raw += text
        prose = strip_code_fences_incremental(self._raw)
        new_prose = prose[self._sent_len:]
        self._sent_len = len(prose)
        if not new_prose:
            return []
        self._buffer += new_prose
        chunks = []
        chunk = self._pop_ready_chunk()
        while chunk:
            chunks.append(chunk)
            chunk = self._pop_ready_chunk()
        return chunks

    def flush(self) -> str | None:
        """The stream ended; return any leftover buffered prose as a final
        chunk (or None if there's nothing left)."""
        text = self._buffer.strip()
        self._buffer = ""
        return text or None

    def _pop_ready_chunk(self, min_len: int = 40, max_len: int = 400) -> str | None:
        if not self._first_chunk_done:
            min_len = self._FIRST_CHUNK_MIN_LEN
        buf = self._buffer
        if len(buf) < min_len:
            return None
        last_boundary = None
        for m in self._SENTENCE_BOUNDARY_RE.finditer(buf):
            if m.end() >= min_len:
                last_boundary = m.end()
        if last_boundary is None:
            if len(buf) >= max_len:
                cut = buf.rfind(" ", 0, max_len)
                last_boundary = cut if cut > 0 else max_len
            else:
                return None
        chunk = buf[:last_boundary].strip()
        self._buffer = buf[last_boundary:]
        self._first_chunk_done = True
        return chunk or None


class WebEvents(AgentEvents):
    """Pushes agent events into the webview as JSON; blocks on permissions.

    One instance per chat: every event is tagged with the chat's session id
    (sid) so the page can route it -- render it live when that chat is the
    active one, or update the sidebar (spinner/unread/permission badge) when
    it's running in the background. Streaming buffers and TTS state are
    per-turn state, which is exactly why instances can't be shared between
    concurrently-running chats. The permission registry IS shared (passed
    in), so Api.permission_response can resolve a prompt from any chat."""

    def __init__(self, sid: str = "", pending: dict | None = None):
        self._sid = sid
        # NOTE: an empty shared registry is still a SHARED registry -- never
        # test this with truthiness, or every chat quietly gets its own dict
        # and cross-chat permission resolution breaks.
        self._pending_shared = pending
        # Underscore-prefixed: pywebview's inject_pywebview() recursively
        # introspects every non-underscore attribute of the js_api object to
        # build the exposed JS surface. A public `window` attribute gets
        # walked into window.native (the WinForms Form), whose
        # AccessibilityObject.Bounds.Empty chain recurses infinitely in
        # pythonnet (Rectangle.Empty returns another Rectangle exposing its
        # own .Empty). That blows the window's UI thread and freezes the
        # app permanently. Leading underscore makes pywebview skip it.
        self._window: webview.Window | None = None
        self._pending: dict[str, dict] = (
            self._pending_shared if self._pending_shared is not None else {})
        self._cfg = None  # set by Api.__init__ to the shared Config instance
        # Set by Api._make_events: called with a short body string when this
        # chat needs the user (permission prompt). Routes to an OS-level
        # toast when the app window isn't focused; None = no-op (tests,
        # the sid-less global sink).
        self.notifier = None

        # -- read-aloud state --------------------------------------------
        # Whether THIS turn reads assistant content aloud, snapshotted once
        # by start_turn() -- toggling read_aloud mid-response never affects
        # a turn already in flight, and never touches TTS at all if it was
        # off when the turn started.
        self.read_aloud_this_turn = False
        self._tts_main = _TtsFeeder()   # the main agent's own replies
        self._tts_sub = _TtsFeeder()    # whichever sub-agent's panel is focused (see active_view)
        # "" = read from the main chat; a sub-agent id = its inspector panel
        # is open and focused, so THAT is what read-aloud reads instead --
        # the user is watching it work while the main agent sits silently
        # waiting on it anyway. Set by the frontend via set_active_view() on
        # every panel open/switch/close.
        self.active_view: str = ""
        self._tts_queue: "queue.Queue" = queue.Queue()
        self._tts_worker_started = False
        self._tts_seq = 0
        # evaluate_js is not safe to call from several threads at once (see
        # emit()) -- previously this was only ever called from a single
        # thread at a time in practice (whichever thread was streaming the
        # model response), but the new background flush thread below and
        # sub-agent worker threads can now genuinely race with it.
        self._evaluate_lock = threading.Lock()

        # -- streaming display buffer --------------------------------------
        # evaluate_js() is a synchronous, blocking round trip through the
        # WebView2 UI thread (Invoke -> ExecuteScriptAsync -> await the
        # result) -- calling it once per raw SSE delta, as this used to do,
        # serializes the *network read* itself behind UI-thread scheduling
        # latency: the streaming loop in api.py can't pull the next chunk
        # off the wire until the previous one's full IPC round trip
        # completes. Buffering deltas and flushing on a timer instead turns
        # many small blocking round trips into far fewer, larger ones.
        self._stream_lock = threading.Lock()
        self._content_buf = ""
        self._reasoning_buf = ""
        # Sub-agent reasoning/content deltas get the same treatment, buffered
        # per sub-agent id. Without this, every token from every parallel
        # sub-agent was its own synchronous evaluate_js round trip -- all
        # contending on _evaluate_lock, each one also blocking that
        # sub-agent's own network read. With 4-6 sub-agents streaming, the
        # whole app crawled.
        self._sub_bufs: dict = {}  # aid -> {"reasoning": str, "content": str}
        self._flush_thread_started = False

    def emit(self, type_: str, **data) -> None:
        if not self._window:
            return
        if self._sid:
            data.setdefault("sid", self._sid)
        payload = json.dumps({"type": type_, **data})
        try:
            # Hand the event to the page's sink. The payload is already
            # JSON-encoded, so it drops straight into the JS call.
            # evaluate_js isn't safe to call from several threads at once
            # (the flush thread, sub-agent worker threads, and whichever
            # thread is running the agent loop can all reach this).
            with self._evaluate_lock:
                self._window.evaluate_js(
                    f"window.GLM && window.GLM.emit({payload});"
                )
        except Exception:
            # A dropped UI update must never take down the agent turn.
            pass

    # streaming ---------------------------------------------------------
    _STREAM_FLUSH_INTERVAL = 0.06  # seconds

    def _ensure_flush_thread(self) -> None:
        if self._flush_thread_started:
            return
        self._flush_thread_started = True
        threading.Thread(target=self._flush_loop, daemon=True).start()

    def _flush_loop(self) -> None:
        while True:
            time.sleep(self._STREAM_FLUSH_INTERVAL)
            self._flush_stream_buffers()

    def _flush_stream_buffers(self) -> None:
        with self._stream_lock:
            content, self._content_buf = self._content_buf, ""
            reasoning, self._reasoning_buf = self._reasoning_buf, ""
            subs = []
            for aid, buf in self._sub_bufs.items():
                if buf["reasoning"] or buf["content"]:
                    subs.append((aid, buf["reasoning"], buf["content"]))
                    buf["reasoning"] = ""
                    buf["content"] = ""
        if reasoning:
            self.emit("reasoning", text=reasoning)
        if content:
            self.emit("content", text=content)
        for aid, r, c in subs:
            if r:
                self.emit("subagent_stream", id=aid, kind="reasoning", text=r)
            if c:
                self.emit("subagent_stream", id=aid, kind="content", text=c)

    def _flush_one_subagent(self, aid) -> None:
        """Flush a single sub-agent's buffered text NOW -- called before any
        of its non-text events (tool_call, stream_start, ...) so those can't
        overtake text that streamed before them."""
        with self._stream_lock:
            buf = self._sub_bufs.get(aid)
            if not buf:
                return
            r, buf["reasoning"] = buf["reasoning"], ""
            c, buf["content"] = buf["content"], ""
        if r:
            self.emit("subagent_stream", id=aid, kind="reasoning", text=r)
        if c:
            self.emit("subagent_stream", id=aid, kind="content", text=c)

    def stream_start(self):
        self._flush_stream_buffers()  # flush any straggler left from a prior round
        self.emit("stream_start")
        self._tts_main.reset()

    def reasoning_delta(self, text):
        with self._stream_lock:
            self._reasoning_buf += text
        self._ensure_flush_thread()

    def content_delta(self, text):
        with self._stream_lock:
            self._content_buf += text
        self._ensure_flush_thread()
        if self.read_aloud_this_turn:
            for chunk in self._tts_main.feed(text):
                self._enqueue_tts_chunk(chunk)

    def stream_end(self):
        self._flush_stream_buffers()  # make sure everything is sent before stream_end
        self.emit("stream_end")
        if self.read_aloud_this_turn:
            trailing = self._tts_main.flush()
            if trailing:
                self._enqueue_tts_chunk(trailing)

    # read-aloud ----------------------------------------------------------
    def start_turn(self, read_aloud: bool) -> None:
        """Called once per user turn (Api.send), before the agent runs."""
        self.read_aloud_this_turn = bool(read_aloud)
        self._tts_seq = 0
        self.emit("tts_reset")

    def set_active_view(self, view: str) -> None:
        """Which live stream read-aloud reads from: "" for the main chat, or
        a sub-agent's id while its inspector panel is open and focused on it.
        Switching drops whatever sub-agent prose was mid-sentence for the OLD
        view -- the user just looked away, so finishing it out loud
        afterward would be confusing, not helpful."""
        view = view or ""
        if view == self.active_view:
            return
        self.active_view = view
        self._tts_sub.reset()

    def _enqueue_tts_chunk(self, text: str) -> None:
        if not text.strip():
            return
        self._ensure_tts_worker()
        # In voice mode this is called from the streaming turn thread AND from
        # worker threads (a spoken permission prompt), so the seq assignment and
        # the queue put must be atomic together -- otherwise a race can assign
        # out-of-order seqs and stall the frontend's in-order playback.
        with self._stream_lock:
            self._tts_seq += 1
            self._tts_queue.put((self._tts_seq, text))

    def _ensure_tts_worker(self) -> None:
        if self._tts_worker_started:
            return
        self._tts_worker_started = True
        threading.Thread(target=self._tts_worker_loop, daemon=True).start()

    def _tts_worker_loop(self) -> None:
        # One worker, strictly serial: chunks are emitted in the order they
        # were enqueued, and a local model pipeline isn't meant for
        # concurrent synthesis calls anyway (see tts._lock).
        from .. import tts_engine
        while True:
            seq, text = self._tts_queue.get()
            engine, voice = _tts_engine_voice(self._cfg)
            speed = (self._cfg.tts_speed if self._cfg else None) or 1.0
            try:
                audio, sr = tts_engine.synthesize(text, voice=voice, speed=speed, engine=engine)
                wav = tts_engine.audio_to_wav_bytes(audio, sr)
                src = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
                self.emit("play_audio", seq=seq, src=src)
            except Exception as e:
                self.emit("play_audio", seq=seq, src="", error=str(e))

    # tools --------------------------------------------------------------
    def tool_call(self, name, args, call_id=""):
        self.emit("tool_call", name=name, args=args, call_id=call_id)

    def tool_result(self, name, content, is_error=False, call_id=""):
        # call_id so the page pairs a result with the chip that call made,
        # instead of with whichever chip it happened to build last.
        self.emit("tool_result", name=name, content=content[:12000],
                  error=is_error, call_id=call_id)

    def todos(self, items):
        self.emit("todos", items=items)

    # notices --------------------------------------------------------------
    def info(self, msg):
        self.emit("notice", level="info", text=msg)

    def toast(self, msg, level="info"):
        """A transient side popup that is NOT saved into the chat (unlike a
        notice). For ephemeral progress like a one-time model download."""
        self.emit("toast", level=level, text=msg)

    def warn(self, msg):
        self.emit("notice", level="warn", text=msg)

    def error(self, msg):
        self.emit("notice", level="error", text=msg)

    @contextmanager
    def status(self, label):
        self.emit("status", active=True, label=label)
        try:
            yield
        finally:
            self.emit("status", active=False, label="")

    def turn_done(self, usage, context=0):
        self.emit("turn_done", prompt_tokens=usage.prompt_tokens,
                  completion_tokens=usage.completion_tokens,
                  context=context)

    # context compaction --------------------------------------------------
    on_compacted = None  # set by Api._make_events to prune the snapshot map

    def compacted(self, summary):
        if self.on_compacted:
            try:
                self.on_compacted()
            except Exception:
                pass
        self.emit("compacted", summary=summary)

    # steering --------------------------------------------------------------
    def steered(self, text):
        self.emit("steered", text=text)

    def steer_returned(self, text):
        self.emit("steer_returned", text=text)

    def wrapup_requested(self):
        self.emit("wrapup_requested")

    # sub-agents ----------------------------------------------------------
    def subagent(self, id, name, status, mission="", summary=""):
        self.emit("subagent", id=id, name=name, status=status,
                  mission=mission, summary=summary)

    def subagent_stream(self, id, kind, **data):
        # Text deltas are by far the highest-frequency events and each emit()
        # is a blocking evaluate_js round trip -- buffer them per sub-agent
        # and let the flush thread batch them (see _flush_stream_buffers).
        if kind in ("reasoning", "content"):
            with self._stream_lock:
                buf = self._sub_bufs.setdefault(id, {"reasoning": "", "content": ""})
                buf[kind] += data.get("text", "")
            self._ensure_flush_thread()
            # Read-aloud only ever reads CONTENT (never reasoning, matching
            # the main agent) from whichever sub-agent's panel is currently
            # focused -- that's the one thing worth hearing while the main
            # chat sits silently waiting on it.
            if kind == "content" and self.read_aloud_this_turn and id == self.active_view:
                for chunk in self._tts_sub.feed(data.get("text", "")):
                    self._enqueue_tts_chunk(chunk)
            return
        # Everything else is rare but must stay ordered relative to the text
        # that streamed before it.
        self._flush_one_subagent(id)
        if kind == "tool_result":
            # Match the main chat's display cap -- the model already got the
            # full content; shipping up to 60KB per blocking IPC call to the
            # UI just to fill a collapsed chip was pure waste.
            data = dict(data, content=str(data.get("content", ""))[:12000])
        elif kind == "stream_start" and id == self.active_view:
            self._tts_sub.reset()
        elif kind == "stream_end" and id == self.active_view and self.read_aloud_this_turn:
            trailing = self._tts_sub.flush()
            if trailing:
                self._enqueue_tts_chunk(trailing)
        self.emit("subagent_stream", id=id, kind=kind, **data)

    # images ----------------------------------------------------------------
    def show_image(self, path, caption=""):
        try:
            src = _data_uri(Path(path))
        except Exception:
            src = ""
        self.emit("show_image", path=str(path), caption=caption or "", src=src)

    # audio -------------------------------------------------------------------
    def show_audio(self, path, caption=""):
        try:
            src = _data_uri(Path(path))
        except Exception:
            src = ""
        self.emit("show_audio", path=str(path), caption=caption or "", src=src)

    # background workers (conversational mode) --------------------------------
    def worker_update(self, id, name, status, summary="", result=""):
        self.emit("worker_update", id=id, name=name, status=status,
                  summary=summary, result=result)

    def worker_permission(self, rid, worker, title, preview, spoken="", always=""):
        # Speak the question (so it's answerable hands-free) and show a card.
        if spoken:
            self._enqueue_tts_chunk(spoken)
        self.emit("worker_permission", rid=rid, worker=worker, title=title,
                  preview=preview[:2000], spoken=spoken, always=always)

    # permissions ------------------------------------------------------------
    def ask_permission(self, title, preview, always_label=None):
        rid = uuid.uuid4().hex
        entry = {"event": threading.Event(), "answer": ("n", "")}
        self._pending[rid] = entry
        self.emit("permission", id=rid, title=title, preview=preview,
                  always=always_label or "")
        if self.notifier:
            try:
                self.notifier(f"Needs permission: {title}")
            except Exception:
                pass
        entry["event"].wait(timeout=3600)
        self._pending.pop(rid, None)
        return entry["answer"]

    def resolve_permission(self, rid, answer, feedback=""):
        entry = self._pending.get(rid)
        if not entry:
            return
        entry["answer"] = ("n", feedback or "") if answer == "n" else answer
        entry["event"].set()
