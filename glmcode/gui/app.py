"""Make No Mistakes desktop app: pywebview window + JS bridge around the agent core."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from pathlib import Path

import webview

from .. import __version__
from ..agent import Agent
from ..api import IMAGE_EXTENSIONS, ZaiClient
from ..backup import BackupRepo
from .. import backup as backup_module
from ..config import (BUILTIN_PROVIDER_NAME, CONFIG_DIR, PERMISSION_MODES, Config,
                      all_providers, find_provider, load_config, save_config)
from ..events import AgentEvents
from .. import githubsync
from .. import qrcode_util
from ..notify import APP_NAME, notify
from ..prompts import EXECUTE_PLAN_MESSAGE, PLAN_MODE_PREAMBLE, TITLE_PROMPT
from ..sessions import SessionStore, new_id, to_display
from ..transcript import Transcript, search_sessions
from ..tools import (configure_search,
                     resolve_mentions as tools_resolve_mentions,
                     build_text_file_context as tools_build_text_file_context,
                     search_project_files as tools_search_project_files)
from ..permissions import add_command_aliases

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_BG = WEB_DIR / "bg-default.jpg"
# Always-available scratch folder for quick, throwaway projects -- a sibling
# of this app's own install directory (e.g. .../Theo/Make No Mistakes ->
# .../Theo/whiteboard), created on first use rather than at import time.
WHITEBOARD_DIR = Path(__file__).resolve().parents[3] / "whiteboard"


# --------------------------------------------------------------------- #

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

    def tool_result(self, name, content, is_error=False):
        self.emit("tool_result", name=name, content=content[:12000], error=is_error)

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


# --------------------------------------------------------------------- #

def _tts_engine_voice(cfg) -> tuple[str, str]:
    """The active TTS engine and its voice from config. Each engine keeps its
    own voice (tts_voice for Kokoro, piper_voice for Piper), so switching
    engines never lands on a voice the other one doesn't have."""
    engine = (getattr(cfg, "tts_engine", "kokoro") if cfg else "kokoro") or "kokoro"
    if engine == "piper":
        return engine, (getattr(cfg, "piper_voice", "") if cfg else "") or "en_US-amy-medium"
    return "kokoro", (getattr(cfg, "tts_voice", "") if cfg else "") or "af_heart"


_PATH_RULE_ACTIONS = ("allow", "ask", "deny")


def _normalize_path_rules(value) -> list:
    """Clean scoped-autonomy rules coming from the UI: keep only entries with a
    non-empty glob and a valid action, de-duplicate, and cap the list."""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            continue
        glob = str(item.get("glob", "")).strip()[:200]
        action = str(item.get("action", "")).strip().lower()
        if not glob or action not in _PATH_RULE_ACTIONS:
            continue
        key = (glob, action)
        if key in seen:
            continue
        seen.add(key)
        out.append({"glob": glob, "action": action})
        if len(out) >= 100:
            break
    return out


def _data_uri(path: Path, max_bytes: int = 12_000_000) -> str:
    data = path.read_bytes()[:max_bytes]
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _thumb_uri(path: Path, size: int = 360) -> str:
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return _data_uri(path)


def persist_env_var(name: str, value: str) -> bool:
    """Persist an env var to the user's environment (Windows `setx`). Best
    effort: the value is ALWAYS set for the current process first, so the app
    works this run no matter what. Persistence is a bonus that must never
    raise -- a locked-down machine (school/corporate Group Policy) can block
    or HANG setx, and a TimeoutExpired here used to kill onboarding entirely."""
    from ..tools import NO_WINDOW_KWARGS
    os.environ[name] = value  # active this run regardless of persistence
    if sys.platform != "win32":
        return False          # setx is Windows-only; not a failure elsewhere
    try:
        r = subprocess.run(["setx", name, value], capture_output=True, timeout=8,
                           **NO_WINDOW_KWARGS)
        return r.returncode == 0
    except Exception:
        # setx missing, blocked by policy, or hung past the timeout -- fine,
        # the key still works this session via os.environ above.
        return False


# --------------------------------------------------------------------- #

class ChatState:
    """Everything one open chat owns: its live agent (which may be mid-turn
    on a background thread), its event sink, and its per-chat settings."""

    def __init__(self, sid: str, agent: Agent, events: WebEvents):
        self.sid = sid
        self.agent = agent
        self.events = events
        self.backup_repo: BackupRepo | None = None
        self.title = ""
        self.provider = ""
        self.model = ""
        self.auto_backup = True
        self.turn_lock = threading.Lock()  # one turn at a time PER CHAT
        # One entry per send-turn, in order: {"commit": <hash or None>}. The
        # list index IS the turn ordinal, so turn_snapshots[k] is the pre-turn
        # file state of the k-th user turn -- what "edit & resend" reverts to.
        # Cleared on compaction (older turns' messages no longer exist).
        self.turn_snapshots: list[dict] = []
        # Speech-to-speech voice mode: a separate, persistent conversational
        # agent (pure delegator -- see Agent(conversational=True)) that shares
        # this chat's project/backup/mcp so its background workers act on the
        # real code. It streams through its OWN events (sid "<sid>::voice") so
        # the voice overlay is cleanly separate from the coding transcript.
        # All lazily created on first use.
        self.convo_agent: Agent | None = None
        self.convo_events: WebEvents | None = None
        self.convo_lock = threading.Lock()  # one voice turn at a time


class Api:
    """Methods callable from JS via window.pywebview.api.*"""

    def __init__(self):
        self._cfg: Config = load_config()
        # Shared across every chat's WebEvents so permission_response can
        # resolve a prompt no matter which chat asked.
        self._perm_registry: dict = {}
        # Sid-less sink for app-level notices before/outside any chat.
        self._events_global = WebEvents("", self._perm_registry)
        self._events_global._cfg = self._cfg
        # Every open chat, live agent included -- chats keep running in the
        # background when the user switches away (see send/_run_send_turn).
        self._chats: dict[str, ChatState] = {}
        # Underscore-prefixed: see the comment on WebEvents._window above —
        # this class is the js_api object pywebview recursively introspects,
        # so a public `window` attribute here triggers the same infinite
        # AccessibilityObject.Bounds.Empty recursion and freezes the app.
        self._window: webview.Window | None = None
        self._store = SessionStore()
        self.session_id: str | None = None
        self._client: ZaiClient | None = None
        # Updated by JS on window focus/blur; gates OS-level toasts (they
        # only fire while the user is away in another app -- the in-app UI
        # already covers the focused case).
        self._window_focused = True

        configure_search(self._cfg.search_provider, self._cfg.resolve_tavily_key())
        # MCP servers: spawned in the background so a slow `npx` download
        # never delays app startup; agents pick the tools up per model call.
        from ..mcp import McpManager
        self._mcp = McpManager(self._cfg)
        if self._cfg.mcp_servers:
            self._mcp.start_all_async()
        # Initialize command aliases for npm/yarn/pnpm/git
        add_command_aliases({
            "npm": "npm",
            "yarn": "npm",
            "pnpm": "npm",
            "git": "git",
        })
        # Scheduled/watched tasks: a lightweight poller fires the ones that are
        # due. Daemon thread, started once; does nothing until the user creates
        # a task (so there's no cost/behavior unless opted in).
        self._sched_stop = threading.Event()
        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        # Codebase memory: honor the neural-search setting from the start.
        from .. import codebase_memory
        codebase_memory.set_neural_enabled(self._cfg.codebase_memory_neural)

    # -- active-chat accessors ------------------------------------------- #
    # Most of this class predates parallel chats and talks about THE agent/
    # events/title; these map that vocabulary onto whichever chat is active.

    @property
    def _active(self) -> "ChatState | None":
        return self._chats.get(self.session_id) if self.session_id else None

    @property
    def _agent(self) -> Agent | None:
        c = self._active
        return c.agent if c else None

    @property
    def _events(self) -> WebEvents:
        c = self._active
        return c.events if c else self._events_global

    @property
    def _backup_repo(self) -> BackupRepo | None:
        c = self._active
        return c.backup_repo if c else None

    @_backup_repo.setter
    def _backup_repo(self, value) -> None:
        if self._active:
            self._active.backup_repo = value

    @property
    def session_title(self) -> str:
        c = self._active
        return c.title if c else ""

    @session_title.setter
    def session_title(self, value: str) -> None:
        if self._active:
            self._active.title = value

    @property
    def auto_backup(self) -> bool:
        c = self._active
        return c.auto_backup if c else True

    @auto_backup.setter
    def auto_backup(self, value: bool) -> None:
        if self._active:
            self._active.auto_backup = bool(value)

    @property
    def session_provider(self) -> str:
        c = self._active
        return c.provider if c else ""

    @session_provider.setter
    def session_provider(self, value: str) -> None:
        if self._active:
            self._active.provider = value

    @property
    def session_model(self) -> str:
        c = self._active
        return c.model if c else ""

    @session_model.setter
    def session_model(self, value: str) -> None:
        if self._active:
            self._active.model = value

    def _ensure_client(self) -> ZaiClient | None:
        key = self._cfg.resolve_api_key()
        if not key:
            return None
        if self._client is None:
            self._client = ZaiClient(key, self._cfg.base_url)
        return self._client

    def _make_events(self, sid: str) -> WebEvents:
        ev = WebEvents(sid, self._perm_registry)
        ev._cfg = self._cfg
        ev._window = self._window
        ev.notifier = lambda body, _sid=sid: self._os_attention(_sid, body)
        # Compaction rewrites history (older turns' messages are replaced by a
        # summary), so their pre-turn snapshot commits no longer line up with
        # any turn -- drop them so "edit & resend" can't revert to a stale one.
        ev.on_compacted = lambda _sid=sid: self._on_compacted(_sid)
        return ev

    def _on_compacted(self, sid: str) -> None:
        cs = self._chats.get(sid)
        if cs:
            cs.turn_snapshots = []

    def _os_attention(self, sid: str, body: str) -> None:
        """OS-level toast for 'this chat needs you': a blocking permission
        prompt, or a finished turn waiting on the user. Titled with the
        chat's name so parallel chats are tellable apart."""
        if self._window_focused or not self._cfg.notifications:
            return
        cs = self._chats.get(sid)
        notify(cs.title if cs and cs.title else APP_NAME, body)

    def set_window_focus(self, focused):
        self._window_focused = bool(focused)
        return {"ok": True}

    # -- lifecycle ------------------------------------------------------- #

    def log(self, msg: str):
        """Let the page drop breadcrumbs into the startup log (see _startup_log).
        Lets us tell a native WebView2 hang (no JS ever runs) apart from a hang
        inside boot() (JS logged 'boot:start' but never 'boot:done')."""
        _startup_log(f"[js] {msg}")
        return {"ok": True}

    def boot(self):
        _startup_log("[py] boot() called")
        has_key = bool(self._cfg.resolve_api_key())
        result = {
            "version": __version__,
            "needsKey": not has_key,
            "background": self.get_background(),
            "settings": self._settings(),
            "sessions": self.list_sessions(),
            "session": None,
            "contextLimit": self._cfg.context_limit_tokens,
        }
        if has_key:
            result["session"] = self._resume_last()
            result["sessions"] = self.list_sessions()
        _startup_log("[py] boot() returning")
        return result

    def _resume_last(self):
        """Reopen the last active session on launch, if any still exists."""
        sid = self._cfg.last_session_id
        data = self._store.load(sid) if sid else None
        if data is None:
            sessions = self.list_sessions()
            if sessions:
                sid = sessions[0]["id"]
                data = self._store.load(sid)
        if data is None:
            self._agent = None
            self.session_id = None
            return None
        return self._activate_session(
            sid, data.get("messages", []), data.get("cwd", ""),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("todos", []), data.get("title", ""),
        )

    def save_api_key(self, key: str):
        key = (key or "").strip()
        if not key:
            return {"error": "empty key"}
        # Every step is defensive: onboarding must ALWAYS complete once a key
        # is entered. The key goes live via os.environ inside persist_env_var,
        # so even if persistence or resuming a prior session fails (a fresh or
        # locked-down machine), the app still opens ready to use.
        try:
            persisted = persist_env_var("ZAI_API_KEY", key)
        except Exception:
            persisted = False
        self._client = None
        session, sessions = None, []
        try:
            session = self._resume_last()
            sessions = self.list_sessions()
        except Exception:
            pass
        return {"ok": True, "persisted": persisted, "session": session,
                "sessions": sessions}

    def win(self, action: str):
        w = self._window
        if not w:
            return
        if action == "close":
            w.destroy()
        elif action == "min":
            w.minimize()
        elif action == "max":
            try:
                w.toggle_fullscreen()
            except Exception:
                pass

    def open_external(self, url: str):
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            webbrowser.open(url)

    def open_path(self, path: str):
        """Open a file/folder the agent mentioned, in whatever the OS has
        associated with it (editor for code, explorer for folders). Only ever
        called from a user's explicit click on a path in the chat."""
        if not isinstance(path, str) or not path.strip():
            return {"error": "empty"}
        p = Path(path.strip()).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            p = p.resolve()
        except OSError:
            return {"error": "bad path"}
        if not p.exists():
            return {"error": "not found"}
        try:
            if sys.platform == "win32":
                os.startfile(str(p))  # noqa: S606 -- user-initiated open
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True}

    # -- settings ---------------------------------------------------------- #

    def _settings(self):
        c = self._cfg
        return {
            "mode": c.mode, "model": c.model, "vision_model": c.vision_model,
            "vision_route": c.vision_route, "thinking": c.thinking,
            "thinking_mode": c.thinking_mode, "verify_edits": c.verify_edits,
            "auto_fix_tests": c.auto_fix_tests, "parallel_attempts": c.parallel_attempts,
            "codebase_memory_neural": c.codebase_memory_neural,
            "show_reasoning": c.show_reasoning, "temperature": c.temperature,
            "cwd": str(Path.cwd()) if self.session_id else "",
            "background_custom": bool(c.background_path),
            "read_aloud": c.read_aloud, "tts_engine": c.tts_engine,
            "tts_voice": c.tts_voice, "piper_voice": c.piper_voice, "tts_speed": c.tts_speed,
            "stt_model": c.stt_model, "stt_language": c.stt_language,
            "voice_sensitivity": c.voice_sensitivity,
            "voice_earcons": c.voice_earcons, "voice_ptt_key": c.voice_ptt_key,
            "voice_silence_ms": c.voice_silence_ms,
            "voice_wake_enabled": c.voice_wake_enabled,
            "voice_wake_word": c.voice_wake_word,
            "voice_wake_gated": c.voice_wake_gated,
            "voice_reply_language": c.voice_reply_language,
            "notifications": c.notifications, "reduce_effects": c.reduce_effects,
            "browser_headless": c.browser_headless,
            "browser_keep_logins": c.browser_keep_logins,
            "browser_provider": c.browser_provider, "browser_model": c.browser_model,
            "path_rules": [dict(r) for r in c.path_rules],
            "github_clone_root": c.github_clone_root,
            "github_auto_pull": c.github_auto_pull, "github_auto_push": c.github_auto_push,
            "phone_app_url": c.phone_app_url,
        }

    def set_setting(self, key: str, value):
        c = self._cfg
        if key == "mode" and value in PERMISSION_MODES:
            if self._agent:
                self._agent.set_mode(value)
            else:
                c.mode = value
        elif key == "vision_route" and value in ("describe", "direct"):
            c.vision_route = value
        elif key == "thinking_mode" and value in ("low", "medium", "high", "max"):
            c.thinking_mode = value
            c.thinking = value != "low"  # keep the derived flag consistent
        elif key in ("thinking", "show_reasoning", "read_aloud", "notifications",
                     "reduce_effects", "browser_headless", "browser_keep_logins",
                     "verify_edits", "auto_fix_tests"):
            setattr(c, key, bool(value))
        elif key in ("model", "vision_model") and isinstance(value, str) and value.strip():
            setattr(c, key, value.strip())
            if key == "model" and self._agent:
                self._agent.rebuild_system_prompt()
        elif key == "tts_engine" and value in ("kokoro", "piper"):
            c.tts_engine = value
        elif key == "tts_voice" and isinstance(value, str) and value.strip():
            c.tts_voice = value.strip()
        elif key == "piper_voice" and isinstance(value, str) and value.strip():
            c.piper_voice = value.strip()
        elif key == "stt_model" and isinstance(value, str) and value.strip():
            c.stt_model = value.strip()
        elif key == "stt_language" and isinstance(value, str):
            c.stt_language = value.strip()
        elif key == "voice_sensitivity":
            try:
                c.voice_sensitivity = min(2.0, max(0.5, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "voice_earcons":
            c.voice_earcons = bool(value)
        elif key == "voice_ptt_key" and isinstance(value, str) and value.strip():
            c.voice_ptt_key = value.strip()[:32]
        elif key == "voice_silence_ms":
            try:
                c.voice_silence_ms = int(min(1600, max(400, float(value))))
            except (TypeError, ValueError):
                pass
        elif key == "voice_wake_enabled":
            c.voice_wake_enabled = bool(value)
        elif key == "voice_wake_word" and isinstance(value, str) and value.strip():
            c.voice_wake_word = value.strip()[:60]
        elif key == "voice_wake_gated":
            c.voice_wake_gated = bool(value)
        elif key == "voice_reply_language" and value in ("en", "match"):
            c.voice_reply_language = value
            # If a voice session is open, refresh its prompt so the change
            # applies right away rather than only on the next session.
            cs = self._active
            if cs is not None and cs.convo_agent is not None:
                try:
                    cs.convo_agent.rebuild_system_prompt()
                except Exception:
                    pass
        elif key == "tts_speed":
            try:
                c.tts_speed = min(2.0, max(0.5, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "temperature":
            try:
                c.temperature = min(1.5, max(0.0, float(value)))
            except (TypeError, ValueError):
                pass
        elif key == "codebase_memory_neural":
            c.codebase_memory_neural = bool(value)
            from .. import codebase_memory
            codebase_memory.set_neural_enabled(c.codebase_memory_neural)
            if c.codebase_memory_neural and not codebase_memory.NeuralEmbedder.packages_installed():
                self._install_neural_memory()   # background; falls back to lexical until ready
        elif key == "parallel_attempts":
            try:
                c.parallel_attempts = int(min(3, max(1, int(value))))
            except (TypeError, ValueError):
                pass
        elif key == "github_clone_root":
            c.github_clone_root = str(value or "").strip()
        elif key == "phone_app_url":
            c.phone_app_url = str(value or "").strip()
        elif key in ("github_auto_pull", "github_auto_push"):
            setattr(c, key, bool(value))
        elif key == "path_rules":
            # Mutate the existing list IN PLACE (c.path_rules[:] = ...) rather
            # than rebinding it: every live agent's PermissionEngine shares this
            # same list object, so in-place update applies the new rules to all
            # open chats immediately.
            c.path_rules[:] = _normalize_path_rules(value)
        else:
            return {"error": f"unknown setting {key}"}
        save_config(c)
        return self._settings()

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

    # -- background ---------------------------------------------------------- #

    def get_background(self) -> str:
        """Data URI for a CUSTOM background only. The DEFAULT background is
        served straight from disk by CSS (#bg loads bg-default.jpg relative to
        the page), so it never depends on this call, boot timing, or the file
        being base64-embeddable -- an empty string means "use the CSS default".
        (Regression: reading/encoding DEFAULT_BG here used to be able to raise
        or come back blank, leaving a fresh install with no background at all.)"""
        try:
            p = Path(self._cfg.background_path) if self._cfg.background_path else None
            if p and p.is_file():
                return _data_uri(p)
        except Exception:
            pass
        return ""

    def pick_background(self):
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)",
                        "All files (*.*)"),
        )
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            return {"error": "not an image file"}
        self._cfg.background_path = str(path)
        save_config(self._cfg)
        return {"background": self.get_background()}

    def reset_background(self):
        self._cfg.background_path = ""
        save_config(self._cfg)
        return {"background": self.get_background()}

    # -- sessions (chat history + per-project work folder) ------------------ #

    def list_sessions(self):
        return self._store.list()

    def search_chats(self, query: str):
        """Sidebar full-text search: matches chat titles and the full
        transcripts (which keep even compacted-away conversation)."""
        return {"sessions": search_sessions(self._store.list(), query)}

    # -- model providers (bring your own model) ------------------------- #

    def providers(self):
        """All providers (built-in + custom) with keys masked, plus the
        current chat's choice."""
        out = []
        for p in all_providers(self._cfg):
            out.append({"name": p["name"], "base_url": p["base_url"],
                        "models": p.get("models") or [],
                        "builtin": bool(p.get("builtin")),
                        "has_key": bool(p.get("api_key"))})
        return {"providers": out,
                "chat_provider": self.session_provider or BUILTIN_PROVIDER_NAME,
                "chat_model": self.session_model or self._cfg.model}

    def add_provider(self, name: str, base_url: str, api_key: str, models: str):
        return self.save_provider("", name, base_url, api_key, models)

    def save_provider(self, original_name: str, name: str, base_url: str,
                      api_key: str, models: str):
        """Add a new API or save edits to an existing one. `original_name`
        is the row the form was opened from ("" = adding a new one).

        Editing the built-in z.ai row only ever means one thing -- setting
        or replacing the API key -- and that key is persisted to the
        ZAI_API_KEY env var (like first-run onboarding), not to the custom
        provider list."""
        original_name = (original_name or "").strip()
        name = (name or "").strip()
        api_key = (api_key or "").strip()
        if BUILTIN_PROVIDER_NAME in (original_name, name):
            if not api_key:
                return {"error": "paste your z.ai API key "
                                 "(free at z.ai → profile → API Keys)"}
            persisted = persist_env_var("ZAI_API_KEY", api_key)
            self._cfg.api_key = api_key  # fallback source if setx failed
            save_config(self._cfg)
            self._client = None  # rebuild with the new key on next use
            res = self.providers()
            res["persisted_env"] = persisted
            return res
        base_url = (base_url or "").strip().rstrip("/")
        model_list = [m.strip() for m in (models or "").split(",") if m.strip()]
        if not name or not base_url or not model_list:
            return {"error": "name, base URL and at least one model id are required"}
        existing = None
        if original_name:
            existing = next((p for p in self._cfg.providers
                             if p.get("name") == original_name), None)
            if existing is None:
                return {"error": f'no API named "{original_name}" to edit'}
        clash = find_provider(self._cfg, name)
        if clash is not None and clash is not existing:
            return {"error": f'an API named "{name}" already exists'}
        # Editing with the key field left empty keeps the stored key.
        entry = {"name": name, "base_url": base_url,
                 "api_key": api_key or (existing or {}).get("api_key", ""),
                 "models": model_list}
        if existing is None:
            self._cfg.providers.append(entry)
        else:
            existing.update(entry)
            if original_name != name:  # chats pointing at the old name follow
                for cs in self._chats.values():
                    if cs.provider == original_name:
                        cs.provider = name
        save_config(self._cfg)
        # The active chat picks up new url/key/models immediately; background
        # and reopened chats re-apply their provider on activation anyway.
        if self.session_provider == name and self._agent and not self._agent.busy:
            keep = self.session_model if self.session_model in model_list else ""
            self._apply_chat_model(self._agent, name, keep)
            self._save_current()
        return self.providers()

    def delete_provider(self, name: str):
        self._cfg.providers = [p for p in self._cfg.providers
                               if p.get("name") != name]
        save_config(self._cfg)
        if self.session_provider == name and self._agent:
            self._apply_chat_model(self._agent, "", "")  # back to the default
            self._save_current()
        return self.providers()

    # -- custom slash commands --------------------------------------------- #

    def commands(self):
        return {"commands": list(self._cfg.commands)}

    def add_command(self, name: str, template: str):
        name = (name or "").strip().lstrip("/").strip()
        template = (template or "").strip()
        if not name or not template:
            return {"error": "both a name and a prompt template are required"}
        if not name.replace("-", "").replace("_", "").isalnum():
            return {"error": "name may only contain letters, numbers, - and _"}
        self._cfg.commands = [c for c in self._cfg.commands if c.get("name") != name]
        self._cfg.commands.append({"name": name, "template": template})
        save_config(self._cfg)
        return self.commands()

    def delete_command(self, name: str):
        self._cfg.commands = [c for c in self._cfg.commands if c.get("name") != name]
        save_config(self._cfg)
        return self.commands()

    # -- export ------------------------------------------------------------ #

    def export_chat(self):
        """Save the active chat as a Markdown file (via a Save dialog)."""
        cs = self._active
        if not cs:
            return {"error": "no active chat"}
        title = cs.title or "chat"
        safe = re.sub(r"[^\w -]+", "", title).strip() or "chat"
        try:
            picked = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=f"{safe}.md")
        except Exception as e:
            return {"error": str(e)}
        if not picked:
            return {"cancelled": True}
        path = Path(picked if isinstance(picked, str) else picked[0])
        try:
            path.write_text(self._chat_markdown(cs), encoding="utf-8")
        except OSError as e:
            return {"error": str(e)}
        return {"ok": True, "path": str(path)}

    def _chat_markdown(self, cs: "ChatState") -> str:
        lines = [f"# {cs.title or 'Conversation'}", "",
                 f"*Project: {cs.agent.workdir}*", ""]
        for it in to_display(cs.agent.messages):
            kind = it.get("kind")
            if kind == "user":
                lines += ["---", "", "### You", "", it.get("text", ""), ""]
            elif kind == "assistant":
                lines += ["### Agent", "", it.get("text", ""), ""]
            elif kind == "tool":
                args = it.get("args", {})
                summary = ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])
                lines += [f"> 🔧 `{it.get('name')}`" + (f" ({summary})" if summary else ""), ""]
            elif kind == "compacted":
                lines += ["> *— context compacted —*", ""]
            elif kind == "steered":
                lines += [f"> ↪ *steered:* {it.get('text', '')}", ""]
        return "\n".join(lines).rstrip() + "\n"

    # -- MCP servers ------------------------------------------------------- #

    def mcp_status(self):
        """Configured MCP servers with live state, for Settings."""
        return {"servers": self._mcp.status()}

    def mcp_add(self, name: str, command: str):
        name = (name or "").strip()
        command = (command or "").strip()
        if not name or not command:
            return {"error": "both a name and a command are required"}
        if any(e.get("name") == name for e in self._cfg.mcp_servers):
            return {"error": f'an MCP server named "{name}" already exists'}
        self._cfg.mcp_servers.append({"name": name, "command": command})
        save_config(self._cfg)
        self._mcp.start_all_async()
        return self.mcp_status()

    def mcp_delete(self, name: str):
        self._cfg.mcp_servers = [e for e in self._cfg.mcp_servers
                                 if e.get("name") != name]
        save_config(self._cfg)
        self._mcp.start_all_async()  # also stops servers dropped from config
        return self.mcp_status()

    def mcp_restart(self, name: str):
        threading.Thread(target=self._mcp.restart, args=(name,),
                         daemon=True).start()
        return {"ok": True}

    def detect_local_providers(self):
        """Probe the well-known local OpenAI-compatible servers (Ollama,
        LM Studio) and add any that respond as providers."""
        import requests as _requests
        added = []
        probes = [
            ("Ollama (local)", "http://localhost:11434/v1",
             "http://localhost:11434/api/tags",
             lambda d: [m["name"] for m in d.get("models", [])]),
            ("LM Studio (local)", "http://localhost:1234/v1",
             "http://localhost:1234/v1/models",
             lambda d: [m["id"] for m in d.get("data", [])]),
        ]
        for name, base_url, probe_url, extract in probes:
            try:
                r = _requests.get(probe_url, timeout=0.8)
                models = extract(r.json())
            except Exception:
                continue
            if not models:
                continue
            existing = find_provider(self._cfg, name)
            if existing and not existing.get("builtin"):
                existing["models"] = models  # refresh the model list
            else:
                self._cfg.providers.append({"name": name, "base_url": base_url,
                                            "api_key": "local", "models": models})
            added.append(f"{name} ({len(models)} models)")
        if added:
            save_config(self._cfg)
        res = self.providers()
        res["found"] = added
        return res

    def set_chat_model(self, provider_name: str, model: str):
        """Switch the CURRENT chat to a provider+model (per chat -- new chats
        keep the free default)."""
        if not self._agent or not self.session_id:
            return {"error": "no active chat"}
        if self._agent.busy:
            return {"error": "can't switch models while the agent is working"}
        if provider_name != BUILTIN_PROVIDER_NAME \
                and not find_provider(self._cfg, provider_name):
            return {"error": f'unknown provider "{provider_name}"'}
        self._apply_chat_model(self._agent, provider_name, model)
        self._save_current()
        return self.providers()

    def _apply_chat_model(self, agent: Agent, provider_name: str, model: str) -> None:
        """Point an agent at the chat's chosen provider+model. Empty/unknown
        provider falls back to the built-in free default."""
        prov = find_provider(self._cfg, provider_name) if provider_name else None
        if prov is None or prov.get("builtin"):
            self.session_provider = ""
            self.session_model = ""
            agent.model_override = None
            agent.vision_client = None
            # Point back at the built-in client too (the agent may have been
            # switched to a custom endpoint earlier in this chat).
            builtin_client = self._ensure_client()
            if builtin_client is not None:
                agent.client = builtin_client
            return
        self.session_provider = prov["name"]
        self.session_model = model or (prov.get("models") or [""])[0]
        agent.client = ZaiClient(prov.get("api_key", ""), prov["base_url"])
        agent.model_override = self.session_model or None
        # Vision keeps working through the built-in provider.
        zai_key = self._cfg.resolve_api_key()
        agent.vision_client = ZaiClient(zai_key, self._cfg.base_url) if zai_key else None

    def _activate_session(self, sid: str, messages: list, cwd: str,
                          prompt_tokens: int, completion_tokens: int,
                          todos: list, title: str = "", auto_backup: bool = True,
                          model_provider: str = "", model: str = "",
                          turn_snapshots: list | None = None) -> dict:
        # A chat that's already open (possibly mid-turn in the background)
        # just becomes the active one -- its live agent, not a disk reload.
        if sid in self._chats:
            return self._switch_to_live(sid)
        cwd_ok = True
        if cwd:
            try:
                os.chdir(cwd)  # for the file-picker dialogs' starting folder
            except OSError:
                cwd_ok = False
        client = self._ensure_client()
        if client is None:
            return {"error": "no API key configured"}
        workdir = Path(cwd) if (cwd and cwd_ok) else Path.cwd()
        events = self._make_events(sid)
        agent = Agent(self._cfg, client, events=events, workdir=workdir)
        agent.mcp = self._mcp
        agent.load_messages(messages)
        agent.set_usage(prompt_tokens, completion_tokens)
        agent.todos = list(todos or [])
        self._chats[sid] = ChatState(sid, agent, events)
        self._chats[sid].turn_snapshots = list(turn_snapshots or [])
        self.session_id = sid
        self.session_title = title
        self.auto_backup = auto_backup
        self._apply_chat_model(agent, model_provider, model)
        self._backup_repo = BackupRepo(sid, workdir) if cwd_ok else None
        agent.backup_repo = self._backup_repo  # powers the review_changes tool
        # Append-only conversation log; rebuild so the system prompt gains
        # the note telling the model these files exist and how to grep them.
        agent.transcript = Transcript(sid, cwd=str(workdir))
        agent.rebuild_system_prompt()
        self._cfg.last_session_id = sid
        save_config(self._cfg)
        if cwd_ok:
            self._maybe_autopull(workdir)  # background pull if this is a connected repo
        return self._session_payload(self._chats[sid])

    def _switch_to_live(self, sid: str) -> dict:
        cs = self._chats[sid]
        self.session_id = sid
        try:
            os.chdir(cs.agent.workdir)
        except OSError:
            pass
        self._cfg.last_session_id = sid
        save_config(self._cfg)
        return self._session_payload(cs)

    def _session_payload(self, cs: "ChatState") -> dict:
        agent = cs.agent
        u = agent.session_usage
        items = to_display(agent.messages)
        for it in items:
            if it.get("kind") in ("tool_image", "tool_audio") and it.get("path"):
                try:
                    it["src"] = _data_uri(Path(it["path"]))
                except OSError:
                    it["src"] = ""  # file moved/deleted since it was shown
        return {
            "ok": True, "id": cs.sid, "cwd": str(agent.workdir),
            "cwd_missing": not agent.workdir.is_dir(),
            "items": items, "todos": agent.todos,
            "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
            "context": agent.context_estimate(),
            "busy": agent.busy,
            "needs_notes": self._needs_project_notes(agent.workdir),
        }

    @staticmethod
    def _needs_project_notes(workdir: Path) -> bool:
        """True for a real project folder that has no agent-notes file yet, so
        the UI can offer to generate one. Skips the whiteboard and empty dirs."""
        try:
            if not workdir.is_dir() or workdir.resolve() == WHITEBOARD_DIR.resolve():
                return False
            from ..prompts import AGENT_MD_NAMES
            if any((workdir / n).is_file() for n in AGENT_MD_NAMES):
                return False
            # Only offer when there's actually code/content to learn.
            for entry in workdir.iterdir():
                if entry.name.startswith("."):
                    continue
                if entry.is_file() or entry.is_dir():
                    return True
            return False
        except OSError:
            return False

    def generate_project_notes(self):
        """Kick off a turn that explores the project and writes a GLM.md."""
        from ..prompts import GLM_MD_TASK
        if self._active is None:
            return {"error": "Open a chat first."}
        self.send(GLM_MD_TASK)
        return {"ok": True}

    def new_session(self, auto_backup: bool = True):
        """Start a brand-new chat. The user picks the project folder themselves —
        nothing is auto-created or defaulted. Other chats keep running."""
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_dir():
            return {"error": "not a folder"}
        res = self._activate_session(new_id(), [], str(path), 0, 0, [], auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        return res

    def open_whiteboard(self, auto_backup: bool = True):
        """Start a brand-new chat in the always-available scratch folder,
        creating it next to this app's own install directory if this is the
        first time it's used. No folder picker -- unlike new_session, there's
        nothing to choose."""
        WHITEBOARD_DIR.mkdir(parents=True, exist_ok=True)
        res = self._activate_session(new_id(), [], str(WHITEBOARD_DIR), 0, 0, [], auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        return res

    def clear_whiteboard(self):
        """Delete everything inside the whiteboard folder (not the folder
        itself, and not any chat history -- purely a filesystem reset for
        the scratch folder's contents)."""
        WHITEBOARD_DIR.mkdir(parents=True, exist_ok=True)
        for child in WHITEBOARD_DIR.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True}

    # -- GitHub integration ------------------------------------------------- #

    def _clone_root(self) -> Path:
        """Where cloned repos land: the configured folder, or the default
        sibling of the app + whiteboard folders."""
        raw = (self._cfg.github_clone_root or "").strip()
        if raw:
            return Path(raw).expanduser()
        return WHITEBOARD_DIR.parent / "repos"

    def _gh_token(self) -> str | None:
        return githubsync.load_token("github.com")

    def github_env(self):
        """Everything the UI needs to render the GitHub controls: whether git is
        present, whether a token is stored (and how securely), and the current
        clone-root / auto-sync settings."""
        store_secure = githubsync.get_store_secure()
        login = self._cfg.extra.get("github_login", "")
        return {
            "available": githubsync.available(),
            "token_present": bool(self._gh_token()),
            "login": login,
            "backend": githubsync.token_backend(),
            "secure": store_secure,
            "clone_root": str(self._clone_root()),
            "auto_pull": bool(self._cfg.github_auto_pull),
            "auto_push": bool(self._cfg.github_auto_push),
        }

    def github_set_token(self, token: str):
        """Verify a token against the GitHub API, then store it securely. The
        raw token is never returned or written to config -- only the resolved
        login name (public) is cached for display."""
        token = (token or "").strip()
        if not token:
            return {"error": "Enter a token."}
        try:
            who = githubsync.verify_token(token)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        githubsync.save_token("github.com", token)
        self._cfg.extra["github_login"] = who.get("login", "")
        save_config(self._cfg)
        return {"ok": True, **self.github_env()}

    def github_forget_token(self):
        githubsync.forget_token("github.com")
        self._cfg.extra.pop("github_login", None)
        save_config(self._cfg)
        return {"ok": True, **self.github_env()}

    def github_list_repos(self):
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        try:
            return {"repos": githubsync.list_repos(token)}
        except githubsync.GitHubError as e:
            return {"error": str(e)}

    def github_status(self):
        """Live sync status of the ACTIVE session's folder (no network)."""
        cs = self._active
        if cs is None:
            return {"connected": False}
        path = Path(cs.agent.workdir)
        try:
            st = githubsync.status(path)
            if st.remote_url:
                try:
                    st.host, st.owner, st.repo = githubsync.parse_repo(st.remote_url)
                except githubsync.GitHubError:
                    pass
            d = st.as_dict()
        except Exception:
            d = {"connected": False}
        d["token_present"] = bool(self._gh_token())
        return d

    def github_clone(self, url: str, auto_backup: bool = True):
        """Clone a repo into the clone-root and open a new session in it."""
        if not githubsync.available():
            return {"error": "git isn't installed or on PATH."}
        try:
            host, owner, repo = githubsync.parse_repo(url)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        token = self._gh_token()
        dest = githubsync.target_dir(self._clone_root(), owner, repo)
        try:
            githubsync.clone(host, owner, repo, dest, token,
                             on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        res = self._activate_session(new_id(), [], str(dest), 0, 0, [],
                                     auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        res["github"] = self.github_status()
        return res

    def github_connect(self, url: str):
        """Mid-session: attach the ACTIVE folder to an existing (often empty)
        repo and push everything up."""
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        try:
            host, owner, repo = githubsync.parse_repo(url)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        token = self._gh_token()
        try:
            githubsync.connect_existing(Path(cs.agent.workdir), host, owner, repo,
                                        token, on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast("Connected to GitHub and synced.", "info")
        return {"ok": True, "github": self.github_status()}

    def github_create_and_connect(self, name: str, private: bool = True):
        """Create a brand-new repo under the user's account, then connect the
        active folder to it and sync -- the smooth 'push this to a new repo' flow."""
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        try:
            made = githubsync.create_repo(token, name, private=private)
            githubsync.connect_existing(
                Path(cs.agent.workdir), "github.com", made["owner"], made["name"],
                token, on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(f"Created {made['full_name']} and synced.", "info")
        return {"ok": True, "github": self.github_status()}

    def github_pull(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        path = Path(cs.agent.workdir)
        token = self._gh_token()
        try:
            # Commit local work first so a rebase pull never fails on a dirty
            # tree (nothing is lost; the user can review the commit).
            githubsync.commit_all(path, "Local changes before pull")
            msg = githubsync.pull(path, token,
                                  on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(msg, "info")
        return {"ok": True, "github": self.github_status()}

    def github_sync(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        token = self._gh_token()
        try:
            msg = githubsync.sync(Path(cs.agent.workdir), token,
                                  message=cs.title or "Update via Make No Mistakes",
                                  on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(msg, "info")
        return {"ok": True, "github": self.github_status()}

    def github_disconnect(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        try:
            githubsync.disconnect(Path(cs.agent.workdir))
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True, "github": self.github_status()}

    # -- PR review -------------------------------------------------------- #

    def _active_repo_coords(self):
        cs = self._active
        if cs is None:
            return None
        try:
            st = githubsync.status(Path(cs.agent.workdir))
            if not st.remote_url:
                return None
            host, owner, repo = githubsync.parse_repo(st.remote_url)
            return host, owner, repo, Path(cs.agent.workdir)
        except Exception:
            return None

    @staticmethod
    def _format_pr_comments(comments) -> str:
        lines = []
        for c in comments:
            loc = f"{c['path']}:{c['line']}" if c.get("path") else "(general)"
            body = (c.get("body") or "").strip()[:600]
            lines.append(f"- [{loc}] {c.get('author', '')}: {body}")
        return "\n".join(lines)

    def github_open_pulls(self):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, _ = coords
        try:
            return {"pulls": githubsync.list_open_pulls(token, owner, repo)}
        except githubsync.GitHubError as e:
            return {"error": str(e)}

    def github_review_pr(self, number):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, _ = coords
        try:
            pr = githubsync.get_pull(token, owner, repo, int(number))
            diff = githubsync.pull_diff(token, owner, repo, int(number))
            comments = githubsync.pull_review_comments(token, owner, repo, int(number))
        except (githubsync.GitHubError, ValueError) as e:
            return {"error": str(e)}
        from ..prompts import PR_REVIEW_TASK
        task = PR_REVIEW_TASK.format(
            number=pr["number"], title=pr["title"], author=pr["author"],
            head=pr["head"], base=pr["base"], body=(pr["body"] or "(no description)")[:2000],
            comments=self._format_pr_comments(comments) or "(none yet)", diff=diff)
        self.send(task)
        return {"ok": True}

    def github_address_pr(self, number):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, workdir = coords
        try:
            pr = githubsync.get_pull(token, owner, repo, int(number))
            githubsync.fetch_pr_branch(workdir, token, int(number), pr.get("head", ""))
            comments = githubsync.pull_review_comments(token, owner, repo, int(number))
        except (githubsync.GitHubError, ValueError) as e:
            return {"error": str(e)}
        from ..prompts import PR_ADDRESS_TASK
        task = PR_ADDRESS_TASK.format(
            number=pr["number"], title=pr["title"],
            comments=self._format_pr_comments(comments) or "(no review comments found)")
        self.send(task)
        return {"ok": True, "github": self.github_status()}

    def github_setup_phone_access(self):
        """Write the GitHub Actions workflow that lets you run the agent from
        your phone into the connected repo, and point the user at the secret
        page. They Sync it up, add the ZAI_API_KEY secret, and can then comment
        /agent from anywhere."""
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        _, owner, repo, workdir = coords
        tmpl = Path(__file__).resolve().parents[2] / "docs" / "agent-workflow.yml"
        try:
            content = tmpl.read_text(encoding="utf-8")
        except OSError:
            return {"error": "Workflow template missing — copy docs/agent-workflow.yml manually."}
        try:
            dest = workdir / ".github" / "workflows" / "agent.yml"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except OSError as e:
            return {"error": f"Couldn't write the workflow: {e}"}
        return {"ok": True, "path": ".github/workflows/agent.yml",
                "secrets_url": f"https://github.com/{owner}/{repo}/settings/secrets/actions/new"}

    def get_phone_app(self):
        """Return the installable phone-app URL plus a scannable QR code (inline
        SVG) so you can open it on your phone. The URL is configurable; the QR is
        generated locally (no network, nothing leaves the machine)."""
        url = (self._cfg.phone_app_url or "").strip()
        if not url:
            return {"url": "", "error": "No phone-app URL set yet."}
        try:
            svg = qrcode_util.qr_svg(url)
        except Exception as e:
            return {"url": url, "error": str(e)}
        return {"url": url, "svg": svg}

    def _maybe_autopull(self, workdir: Path) -> None:
        """Background best-effort pull when opening a connected session. Skips a
        dirty tree (never touches uncommitted local work automatically)."""
        if not self._cfg.github_auto_pull:
            return
        ev = self._events  # capture now; the active chat may change later
        def work():
            try:
                st = githubsync.status(workdir)
                if not st.connected or st.dirty:
                    return
                token = self._gh_token()
                out = githubsync.pull(workdir, token)
                if "up to date" not in out.lower():
                    ev.toast("Pulled latest from GitHub.", "info")
            except Exception:
                pass  # opening a chat must never fail because of a pull
        threading.Thread(target=work, daemon=True).start()

    def _maybe_autopush(self, cs: "ChatState") -> None:
        """Background best-effort commit+push after a turn that changed files."""
        if not self._cfg.github_auto_push:
            return
        workdir = Path(cs.agent.workdir)
        ev = cs.events
        def work():
            try:
                st = githubsync.status(workdir)
                if not st.connected or not (st.dirty or st.ahead > 0):
                    return
                token = self._gh_token()
                githubsync.sync(workdir, token,
                                message=cs.title or "Update via Make No Mistakes")
                ev.toast("Synced changes to GitHub.", "info")
            except githubsync.GitHubError as e:
                ev.toast(f"GitHub sync failed: {e}", "warn")
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    # -- scheduled & watched tasks ----------------------------------------- #

    def scheduled_tasks(self):
        from .. import scheduler as sched
        return {"tasks": [{**t, "desc": sched.describe(t)} for t in self._cfg.scheduled_tasks]}

    def save_scheduled_task(self, task: dict):
        from .. import scheduler as sched
        norm = sched.normalize_task(task or {})
        if norm is None:
            return {"error": "That task is missing a prompt, folder, or a valid schedule."}
        tasks = self._cfg.scheduled_tasks
        # For a watch task, record the current baseline so it fires on the NEXT
        # change, not immediately.
        if norm["schedule"]["kind"] == "watch" and not norm["last_sig"]:
            norm["last_sig"] = sched.folder_signature(norm["schedule"]["path"])
        for i, t in enumerate(tasks):
            if t.get("id") == norm["id"]:
                tasks[i] = norm
                break
        else:
            if len(tasks) >= sched.MAX_TASKS:
                return {"error": "You have the maximum number of scheduled tasks."}
            tasks.append(norm)
        save_config(self._cfg)
        return self.scheduled_tasks()

    def delete_scheduled_task(self, task_id: str):
        self._cfg.scheduled_tasks = [t for t in self._cfg.scheduled_tasks
                                     if t.get("id") != task_id]
        save_config(self._cfg)
        return self.scheduled_tasks()

    def set_scheduled_enabled(self, task_id: str, enabled: bool):
        for t in self._cfg.scheduled_tasks:
            if t.get("id") == task_id:
                t["enabled"] = bool(enabled)
        save_config(self._cfg)
        return self.scheduled_tasks()

    def run_scheduled_task_now(self, task_id: str):
        for t in self._cfg.scheduled_tasks:
            if t.get("id") == task_id:
                self._fire_scheduled_task(t)
                t["last_run"] = time.time()
                save_config(self._cfg)
                return {"ok": True}
        return {"error": "task not found"}

    def pick_task_folder(self):
        try:
            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:
            picked = None
        if not picked:
            return {"cancelled": True}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        return {"path": str(path)}

    def _install_neural_memory(self) -> None:
        """Background pip-install of the local embedding model package the first
        time neural code search is turned on. Until it's ready, search_code just
        uses the lexical index, so nothing breaks meanwhile."""
        ev = self._events

        def work():
            import sys as _sys
            from ..tools import NO_WINDOW_KWARGS
            try:
                ev.toast("Setting up semantic code search (one-time model download)…", "info")
                proc = subprocess.run(
                    [_sys.executable, "-m", "pip", "install", "--user", "--upgrade",
                     "sentence-transformers"],
                    capture_output=True, text=True, timeout=900, **NO_WINDOW_KWARGS)
                from .. import codebase_memory
                if proc.returncode == 0 and codebase_memory.NeuralEmbedder.packages_installed():
                    ev.toast("Semantic code search is ready.", "info")
                else:
                    ev.toast("Couldn't install the embedding model; using keyword search "
                             "instead. You can turn this off in Settings.", "warn")
            except Exception:
                ev.toast("Couldn't set up semantic code search; using keyword search.", "warn")
        threading.Thread(target=work, daemon=True).start()

    def _scheduler_loop(self) -> None:
        from .. import scheduler as sched
        while not self._sched_stop.wait(30):
            try:
                tasks = self._cfg.scheduled_tasks
                if not tasks:
                    continue
                now = time.time()
                dirty = False
                for t in tasks:
                    kind = t.get("schedule", {}).get("kind")
                    sig = (sched.folder_signature(t["schedule"]["path"])
                           if kind == "watch" else None)
                    if t.get("enabled", True) and sched.is_due(t, now, sig):
                        self._fire_scheduled_task(t)
                        t["last_run"] = now
                        if kind == "watch":
                            t["last_sig"] = sig
                        dirty = True
                    elif kind == "watch" and sig and not t.get("last_sig"):
                        t["last_sig"] = sig   # establish the baseline
                        dirty = True
                if dirty:
                    save_config(self._cfg)
            except Exception:
                pass   # a scheduler hiccup must never take the app down

    def _fire_scheduled_task(self, task: dict) -> None:
        """Run a task's prompt headlessly in its project folder as a background
        chat (shows up in the sidebar), then notify. Best-effort."""
        cwd = task.get("cwd", "")
        if not cwd or not Path(cwd).is_dir():
            return
        client = self._ensure_client()
        if client is None:
            return
        sid = new_id()
        workdir = Path(cwd)
        events = self._make_events(sid)
        agent = Agent(self._cfg, client, events=events, workdir=workdir)
        agent.mcp = self._mcp
        cs = ChatState(sid, agent, events)
        cs.title = (task.get("name") or "Scheduled task")[:60]
        self._chats[sid] = cs
        if backup_module.available():
            cs.backup_repo = BackupRepo(sid, workdir)
            agent.backup_repo = cs.backup_repo
        agent.transcript = Transcript(sid, cwd=str(workdir))
        agent.rebuild_system_prompt()

        def work():
            try:
                agent.run_turn({"role": "user", "content": task["prompt"]})
            except Exception as e:
                events.error(f"scheduled task failed: {e}")
            finally:
                self._save_chat(cs)
                try:
                    self._maybe_autopush(cs)
                except Exception:
                    pass
                try:
                    events.emit("bg_refresh", sessions=self.list_sessions())
                except Exception:
                    pass
                notify(APP_NAME, f"Scheduled task “{cs.title}” finished.")
        threading.Thread(target=work, daemon=True).start()

    def open_session(self, sid: str):
        # Live chats (running or not) switch instantly; others load from disk.
        if sid in self._chats:
            res = self._switch_to_live(sid)
            res["sessions"] = self.list_sessions()
            return res
        data = self._store.load(sid)
        if not data:
            return {"error": "session not found"}
        res = self._activate_session(
            sid, data.get("messages", []), data.get("cwd", ""),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("todos", []), data.get("title", ""),
            auto_backup=data.get("auto_backup", True),
            model_provider=data.get("model_provider", ""),
            model=data.get("model", ""),
            turn_snapshots=data.get("turn_snapshots", []),
        )
        res["sessions"] = self.list_sessions()
        return res

    def delete_session(self, sid: str):
        live = self._chats.get(sid)
        if live and live.agent.busy:
            return {"error": "that chat is still working — stop it first"}
        if live:
            live.agent.close_browser()  # don't leak a control_chrome window
        self._chats.pop(sid, None)
        self._store.delete(sid)
        Transcript(sid).delete()  # its transcript goes with it
        closed_active = sid == self.session_id
        if closed_active:
            self.session_id = None
            if self._cfg.last_session_id == sid:
                self._cfg.last_session_id = ""
                save_config(self._cfg)
        return {"ok": True, "sessions": self.list_sessions(), "closed_active": closed_active}

    def _save_chat(self, cs: "ChatState") -> None:
        """Persist ONE chat -- callable from its own turn thread, so a
        background chat saves itself without touching the active one."""
        u = cs.agent.session_usage
        self._store.save(cs.sid, str(cs.agent.workdir), cs.agent.messages,
                         u.prompt_tokens, u.completion_tokens,
                         todos=cs.agent.todos, title=cs.title,
                         auto_backup=cs.auto_backup,
                         model_provider=cs.provider, model=cs.model,
                         turn_snapshots=cs.turn_snapshots)

    def _save_current(self) -> None:
        if self._active:
            self._save_chat(self._active)

    # -- backups (per-chat shadow git repo) --------------------------------- #

    def backup_status(self):
        available = backup_module.available()
        snapshots = []
        if available and self._backup_repo:
            snapshots = [
                {"commit": s.commit, "message": s.message, "timestamp": s.timestamp}
                for s in reversed(self._backup_repo.list_snapshots())
            ]
        return {"available": available, "enabled": self.auto_backup, "snapshots": snapshots}

    def set_backup_enabled(self, enabled: bool):
        self.auto_backup = bool(enabled)
        self._save_current()
        return {"ok": True}

    def turn_changes(self):
        """Per-file changes since the pre-turn snapshot, for the review card
        shown after each turn. Empty when backups are off (no baseline)."""
        if not (self.auto_backup and self._backup_repo):
            return {"files": []}
        try:
            return {"files": self._backup_repo.turn_changes()}
        except Exception:
            return {"files": []}

    def revert_change(self, path: str):
        """Revert ONE file to its pre-turn state (from the review card)."""
        if not self._backup_repo:
            return {"error": "no active chat"}
        if self._agent and self._agent.busy:
            return {"error": "can't revert while the agent is working"}
        try:
            self._backup_repo.revert_file(path)
        except Exception as e:
            return {"error": str(e)}
        return self.turn_changes()

    def revert_backup(self, commit: str):
        if not self._backup_repo:
            return {"error": "no active chat"}
        if self._agent and self._agent.busy:
            return {"error": "can't revert while the agent is working"}
        try:
            self._backup_repo.revert_to(commit)
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}

    def rewind_to(self, turn_ordinal):
        """Edit & resend: rewind the active chat to just before one of your
        past messages -- revert the project files to that turn's pre-turn
        snapshot and truncate the conversation there -- so the JS can re-send
        the edited text as a fresh turn. `turn_ordinal` is the message's
        send-turn number (its position among your messages, counting from 0);
        the absolute truncation point is resolved from it here, so the JS only
        has to count user bubbles -- no per-bubble bookkeeping to drift."""
        cs = self._active
        if not cs:
            return {"error": "no active chat"}
        if cs.agent.busy:
            return {"error": "can't edit a message while the agent is working"}
        agent = cs.agent
        try:
            turn_ordinal = int(turn_ordinal)
        except (TypeError, ValueError):
            return {"error": "bad message reference"}
        # Resolve the turn ordinal to an absolute message position via the same
        # display mapping the JS sees, so the two can't disagree.
        msg_index = next((it["msg_index"] for it in to_display(agent.messages)
                          if it.get("kind") == "user"
                          and it.get("turn_ordinal") == turn_ordinal), None)
        if msg_index is None or not (0 <= msg_index < len(agent.messages)) \
                or agent.messages[msg_index].get("role") != "user":
            return {"error": "that message is no longer available"}

        # Revert files to how they looked right before this turn ran, if we
        # have that snapshot (backups may have been off for it, or it predates
        # a compaction that cleared the map).
        reverted = False
        had_snapshot = 0 <= turn_ordinal < len(cs.turn_snapshots)
        commit = cs.turn_snapshots[turn_ordinal]["commit"] if had_snapshot else None
        if commit and cs.backup_repo:
            try:
                cs.backup_repo.revert_to(commit)
                reverted = True
            except Exception as e:
                return {"error": f"couldn't revert the project files: {e}"}

        # Rewind the conversation and the snapshot map to this point. The JS
        # re-sends the edited text next, which appends a fresh turn (and a
        # fresh snapshot) from here.
        del agent.messages[msg_index:]
        del cs.turn_snapshots[turn_ordinal:]
        agent.todos = []  # any checklist from the undone turns is stale now
        self._save_chat(cs)
        payload = self._session_payload(cs)
        payload["reverted"] = reverted
        payload["had_snapshot"] = bool(commit)
        return payload

    def _generate_title(self, first_message: str) -> str:
        """Ask the model for a short chat name from the first user message.
        Best-effort: any failure just falls back to the derived title."""
        client = self._ensure_client()
        if not client or not first_message.strip():
            return ""
        try:
            res = client.chat(
                model=self._cfg.model,
                messages=[{"role": "user",
                           "content": TITLE_PROMPT.format(message=first_message[:2000])}],
                tools=None, temperature=0.3, max_tokens=24, thinking=False,
            )
            title = " ".join((res.content or "").split()).strip().strip('"\'').rstrip(".")
            return title[:64]
        except Exception:
            return ""

    # -- attachments ------------------------------------------------------ #

    def pick_files(self):
        """Pick any file(s) to attach -- not just images. Copied into the
        project's uploads/ folder on send (see Agent.attach_files); only
        image files get a real thumbnail here, others show a generic icon."""
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("All files (*.*)",),
        )
        if not picked:
            return []
        out = []
        for p in picked:
            path = Path(p)
            if not path.is_file():
                continue
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS
            out.append({"path": str(path), "name": path.name,
                        "thumb": _thumb_uri(path) if is_image else ""})
        return out

    def attach_paths(self, paths: list):
        """Turn dropped-file paths (drag & drop onto the window) into the
        same attachment shape pick_files returns, so the composer pipeline
        treats them identically."""
        out = []
        for p in paths or []:
            path = Path(str(p))
            if not path.is_file():
                continue
            is_image = path.suffix.lower() in IMAGE_EXTENSIONS
            out.append({"path": str(path), "name": path.name,
                        "thumb": _thumb_uri(path) if is_image else ""})
        return out

    def paste_image(self, data_url: str):
        """A screenshot pasted into the composer (Win+Shift+S -> Ctrl+V)
        arrives as a base64 data URL from the JS paste handler. Save it to a
        real file under ~/.makenomistakes/pasted/ so it flows through the
        exact same attachment -> uploads/ pipeline as picked/dropped files."""
        try:
            head, _, b64 = str(data_url or "").partition(",")
            if not b64 or not head.startswith("data:image/"):
                return {"error": "Clipboard did not contain an image."}
            ext = {"data:image/png": ".png", "data:image/jpeg": ".jpg",
                   "data:image/gif": ".gif", "data:image/webp": ".webp",
                   "data:image/bmp": ".bmp"}.get(head.split(";")[0], ".png")
            raw = base64.b64decode(b64)
            if len(raw) > 30_000_000:
                return {"error": "Pasted image is too large (>30MB)."}
            folder = CONFIG_DIR / "pasted"
            folder.mkdir(parents=True, exist_ok=True)
            name = time.strftime("pasted-%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}{ext}"
            path = folder / name
            path.write_bytes(raw)
            return {"path": str(path), "name": name, "thumb": _thumb_uri(path)}
        except Exception as e:
            return {"error": f"Couldn't save pasted image: {e}"}

    def _on_drop(self, event):
        """Native file drop handler (bound in main() to
        window.dom.document.events.drop). pywebview resolves each dropped
        file's real disk path into pywebviewFullPath on the PYTHON-side event
        only -- JS can't see it -- so the actual attaching happens here, then
        we hand the resolved attachments back to the page."""
        try:
            files = ((event or {}).get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files
                     if isinstance(f, dict) and f.get("pywebviewFullPath")]
            atts = self.attach_paths(paths) if paths else []
            if self._window:
                self._window.evaluate_js(
                    "window.__onDropResult(" + json.dumps(atts) + ")")
        except Exception:
            pass

    def list_project_files(self, query: str = ""):
        """Fuzzy file search in the active chat's project, for the composer's
        @-mention picker. Fast (the file list is cached per folder)."""
        cs = self._active
        if not cs:
            return {"files": []}
        try:
            files = tools_search_project_files(cs.agent.workdir, query or "", limit=30)
        except Exception:
            files = []
        return {"files": files}

    # -- chat ---------------------------------------------------------- #

    def send(self, text: str, file_paths: list | None = None, plan: bool = False):
        """Start a turn in the ACTIVE chat and return immediately -- the turn
        runs on its own thread, so the user can switch to (or create) other
        chats while it works. Completion arrives as a "turn_complete" event
        tagged with the chat's sid."""
        cs = self._active
        if cs is None:
            return {"error": "no active chat — start a New Chat first"}
        text = (text or "").strip()
        paths = [Path(p) for p in (file_paths or []) if Path(p).is_file()]
        if not text and not paths:
            return {"error": "empty"}
        if not cs.turn_lock.acquire(blocking=False):
            return {"error": "busy"}
        threading.Thread(target=self._run_send_turn,
                         args=(cs, text, paths, plan), daemon=True).start()
        return {"ok": True, "started": True}

    def _run_send_turn(self, cs: "ChatState", text: str, paths: list,
                       plan: bool) -> None:
        """The body of one chat turn, on its own thread. Everything here uses
        `cs`, never the active-chat accessors -- the user may be looking at a
        completely different chat by the time this finishes."""
        agent, events = cs.agent, cs.events
        raw_text = text  # original user text, for title generation
        ok = False
        try:
            events.emit("chat_busy")
            # @-mentioned files. Two things happen here: the "@" is stripped from
            # every mention that resolves to a real file (so the model gets a
            # clean path like generated/x.jpg, not "@generated/x.jpg" which it
            # would try to open literally), text files have their contents
            # inlined, and image files are collected to embed (direct mode) or
            # left as a clean path for view_image (describe mode). Best-effort.
            mention_images: list = []
            try:
                mentions = tools_resolve_mentions(agent.workdir, text)
                for mn in mentions:
                    text = text.replace("@" + mn["token"], mn["rel"])
                text_files = [(mn["rel"], mn["path"]) for mn in mentions
                              if not mn["is_image"]]
                mention_images = [mn["path"] for mn in mentions if mn["is_image"]]
                file_ctx = tools_build_text_file_context(text_files)
            except Exception:
                file_ctx = ""
            if plan and text:
                # Read-only planning turn: the preamble sets expectations and
                # permissions.plan_only (below) makes them non-negotiable.
                text = PLAN_MODE_PREAMBLE.format(text=text)
            # File contents are appended after the marker so to_display keeps
            # them off the on-screen message.
            if file_ctx:
                text = text + file_ctx
            if paths or mention_images:
                msg = agent.attach_uploads(text, paths, embed_images=mention_images)
            else:
                msg = {"role": "user", "content": text}
            agent.permissions.plan_only = bool(plan and text)
            # File backup: commit the project's current state (i.e. how it
            # looked right before this message's own edits) so "revert to
            # here" later can put it back. Best-effort -- a backup failure
            # must never block sending a message. One turn_snapshots entry is
            # recorded PER turn regardless (commit None when backups are off),
            # so its index stays the turn ordinal that "edit & resend" uses.
            commit = None
            if cs.auto_backup and cs.backup_repo:
                try:
                    # Visible in the status chip: on a big project the git
                    # snapshot can take a moment, and silent pre-turn latency
                    # reads as "the app is slow" rather than "it's working".
                    with events.status("backing up project files..."):
                        commit = cs.backup_repo.snapshot(text or "(files attached)")
                except Exception as e:
                    events.warn(f"backup snapshot failed: {e}")
            cs.turn_snapshots.append({"commit": commit})
            # Snapshot the read-aloud toggle for this turn only: if it's off
            # right now, TTS is never touched below, even if the user flips
            # it mid-response; if it's on, it stays on for this whole turn
            # regardless of later toggling.
            events.start_turn(self._cfg.read_aloud)
            agent.run_turn(msg)
            # First turn of a fresh chat: let the model name it for the sidebar.
            if not cs.title and raw_text:
                t = self._generate_title(raw_text)
                if t:
                    cs.title = t
                    if agent.transcript:
                        # searchable by topic, not just by session id
                        agent.transcript.set_title(t)
            ok = True
        except Exception as e:
            events.error(f"{type(e).__name__}: {e}")
        finally:
            agent.permissions.plan_only = False  # never outlive the turn
            self._save_chat(cs)
            cs.turn_lock.release()
            u = agent.session_usage
            events.emit("turn_complete", ok=ok, plan=bool(plan),
                        prompt_tokens=u.prompt_tokens,
                        completion_tokens=u.completion_tokens,
                        context=agent.context_estimate(),
                        title=cs.title, sessions=self.list_sessions())
            self._maybe_autopush(cs)  # background commit+push if connected
            self._os_attention(cs.sid, "Done -- waiting for you."
                               if ok else "Stopped on an error -- waiting for you.")

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
        threading.Thread(target=self._run_convo_turn,
                         args=(cs, {"role": "user", "content": note}),
                         daemon=True).start()
        return {"ok": True, "started": True}

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

    def _persist_voice_turn(self, cs: "ChatState", user_text: str) -> None:
        """Append a completed voice exchange to the chat's searchable transcript
        (the same append-only log the coding agent uses), so a voice
        conversation isn't lost when the overlay closes -- and the coding agent
        can even grep it later. Best-effort."""
        tr = getattr(cs.agent, "transcript", None)
        if tr is None:
            return
        try:
            reply = self._last_convo_reply(cs)
            if user_text:
                tr.user(user_text, label="Voice")
            if reply:
                tr.assistant(reply)
        except Exception:
            pass

    @staticmethod
    def _last_convo_reply(cs: "ChatState") -> str:
        for m in reversed(cs.convo_agent.messages):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) \
                    and m["content"].strip():
                return m["content"].strip()
        return ""

    def execute_plan(self):
        """The 'Execute plan' button: a normal (non-plan) turn with a canned
        instruction to carry out the plan the user just approved."""
        return self.send(EXECUTE_PLAN_MESSAGE)

    def cancel(self):
        if self._agent:
            self._agent.request_cancel()
        return {"ok": True}

    def stop_powershell(self, call_id: str):
        """Stop one blocking shell command (the Stop button on its chat box)
        by killing its process tree, so the agent's turn -- stuck waiting on
        a command that never exits, like a dev server -- unblocks at once.
        Process-global registry keyed by unique per-call tokens, so this
        reaches the right command even across parallel chats."""
        from ..tools import stop_foreground
        return {"ok": bool(stop_foreground(call_id or ""))}

    def steer(self, text: str):
        text = (text or "").strip()
        if not text:
            return {"error": "empty"}
        if not self._agent or not self._agent.busy:
            return {"error": "nothing running to steer"}
        if not self._agent.steer(text):
            return {"error": "a steering message is already queued"}
        return {"ok": True}

    def steer_clear(self):
        if self._agent:
            self._agent.clear_steer()
        return {"ok": True}

    def steer_subagent(self, aid: str, text: str):
        text = (text or "").strip()
        if not text:
            return {"error": "empty"}
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.steer_subagent(aid, text):
            return {"error": "that sub-agent is no longer running, or already has a queued message"}
        return {"ok": True}

    def steer_subagent_clear(self, aid: str):
        if self._agent:
            self._agent.clear_steer_subagent(aid)
        return {"ok": True}

    def wrapup_subagent(self, aid: str):
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.wrapup_subagent(aid):
            return {"error": "that sub-agent is no longer running"}
        return {"ok": True}

    def set_active_view(self, view: str = ""):
        """Tell read-aloud which live stream to read from: '' for the main
        chat, or a sub-agent's id while its inspector panel is focused on it.
        The frontend calls this on every panel open/switch/close."""
        self._events.set_active_view(view or "")
        return {"ok": True}

    def set_browser_model(self, provider_name: str, model: str):
        """Pick the dedicated Browser Agent model ('' + '' = same as chat).
        Driving a page is the hardest thing the small free model does, so
        routing just control_chrome to a stronger configured model is the
        single biggest browsing-reliability lever."""
        provider_name = (provider_name or "").strip()
        model = (model or "").strip()
        if provider_name and not find_provider(self._cfg, provider_name):
            return {"error": f'unknown provider "{provider_name}"'}
        self._cfg.browser_provider = provider_name
        self._cfg.browser_model = model if provider_name else ""
        save_config(self._cfg)
        return {"ok": True, "browser_provider": self._cfg.browser_provider,
                "browser_model": self._cfg.browser_model}

    def clear_browser_profile(self):
        """Delete the saved agent-browser profile (cookies, logins). The
        escape hatch that keeps 'Remember browser logins' from being a
        one-way door. Refuses while any chat's browser is open on it."""
        for cs in self._chats.values():
            sess = getattr(cs.agent, "browser_session", None)
            if sess is not None and sess.is_open:
                return {"error": "a chat's browser is still open — close it first"}
        p = CONFIG_DIR / "browser-profile"
        try:
            if p.exists():
                shutil.rmtree(p)
        except OSError as e:
            return {"error": f"couldn't delete the profile: {e}"}
        return {"ok": True}

    def pause_browser(self):
        """Freeze the running Browser Agent so the user can take over the
        browser window; resume_browser continues the same agent."""
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.pause_browser_agent():
            return {"error": "no browser agent is running"}
        return {"ok": True}

    def resume_browser(self):
        if not self._agent:
            return {"error": "no active chat"}
        if not self._agent.resume_browser_agent():
            return {"error": "no browser agent is running"}
        return {"ok": True}

    def permission_response(self, rid: str, answer: str, feedback: str = ""):
        self._events.resolve_permission(rid, answer, feedback)
        return {"ok": True}

    def clear_chat(self):
        """Start a fresh chat in the same project folder; the old conversation
        stays in history (nothing is discarded)."""
        if self._agent and self._agent.busy:
            return {"error": "busy"}
        if not self.session_id:
            return {"error": "no active chat"}
        cwd = str(self._agent.workdir) if self._agent else str(Path.cwd())
        # Don't delete the old session — it stays in the sidebar as history
        res = self._activate_session(new_id(), [], cwd, 0, 0, [])
        res["sessions"] = self.list_sessions()  # refresh sidebar
        return res

    def compact_chat(self):
        if not self._agent or self._agent.busy:
            return {"error": "busy or not ready"}
        try:
            note = self._agent.compact()
            self._save_current()
            return {"ok": True, "note": note, "sessions": self.list_sessions(),
                    "context": self._agent.context_estimate()}
        except Exception as e:
            return {"error": str(e)}

    def usage(self):
        if not self._agent:
            return {"prompt_tokens": 0, "completion_tokens": 0, "context": 0}
        u = self._agent.session_usage
        return {"prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "context": self._agent.context_estimate()}


# --------------------------------------------------------------------- #

def _show_error(title: str, message: str) -> None:
    """Show a visible error dialog even under pythonw (no console)."""
    try:
        import tkinter.messagebox as mb
        mb.showerror(title, message)
    except Exception:
        try:
            from pathlib import Path
            (Path.home() / ".makenomistakes" / "crash.log").write_text(
                f"{title}\n\n{message}", encoding="utf-8"
            )
        except OSError:
            pass


GUI_DIR = Path(__file__).parent          # glmcode/gui/
ICO_PATH = GUI_DIR / "app_icon.ico"     # pre-built, ships with package

STARTUP_LOG = Path.home() / ".makenomistakes" / "startup.log"


def _startup_log(stage: str) -> None:
    """Append a timestamped breadcrumb so a silent startup hang is locatable.

    A "not responding" freeze prints no traceback, so we can't rely on the
    crash handler. Instead each startup stage drops a line here; whatever
    stage is *last* in the file is where it hung. The file is truncated at
    the start of every launch so it always reflects the most recent run.
    """
    try:
        from datetime import datetime
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STARTUP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')}  {stage}\n")
    except OSError:
        pass


def main():
    # Fresh breadcrumb trail for this launch (see _startup_log).
    try:
        STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        STARTUP_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass
    _startup_log(f"main() start  platform={sys.platform}  python={sys.version.split()[0]}")

    # Give the process its own taskbar identity. Without an explicit
    # AppUserModelID, Windows groups the window under pythonw.exe and can't
    # attach our window icon to the taskbar button (it shows a blank/generic
    # icon). Setting this makes the window's own icon appear on the taskbar.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "MakeNoMistakes.DesktopApp"
            )
            _startup_log("set AppUserModelID")
        except Exception as e:
            _startup_log(f"AppUserModelID failed: {e}")

    # Verify the web assets exist (common issue if files are missing)
    index = WEB_DIR / "index.html"
    if not index.is_file():
        _startup_log(f"ABORT missing web assets: {index}")
        _show_error("Make No Mistakes",
                    f"Missing web assets: {index}\n"
                    f"Make sure the glmcode/gui/web/ folder was extracted correctly.")
        return

    _startup_log("creating Api()")
    api = Api()

    _startup_log("creating window")
    window = webview.create_window(
        title="Make No Mistakes",
        url=str(index),
        js_api=api,
        width=1240,
        height=820,
        min_size=(880, 600),
        frameless=True,
        easy_drag=False,
        background_color="#0a0d16",
    )
    api._window = window
    api._events_global._window = window
    for cs in api._chats.values():  # chats created before the window existed
        cs.events._window = window

    # Native file drag & drop: the dropped files' real disk paths are only
    # available in pywebview's PYTHON-side drop event (as pywebviewFullPath),
    # so bind a Python handler once the DOM is ready. Best-effort -- older
    # pywebview without DOM-event support just leaves the paperclip button.
    def _bind_drop(w=window):
        try:
            from webview.dom import DOMEventHandler
            w.dom.document.events.drop += DOMEventHandler(
                api._on_drop, prevent_default=True, stop_propagation=False)
            _startup_log("native drop handler bound")
        except Exception as e:
            _startup_log(f"native drop unavailable: {type(e).__name__}: {e}")
    try:
        window.events.loaded += _bind_drop
    except Exception as e:
        _startup_log(f"could not subscribe loaded for drop: {e}")

    # Build webview.start() kwargs
    start_kwargs = dict(debug="--debug" in sys.argv)

    # Deliberately NOT setting a persistent storage_path: a WebView2 profile
    # that survives across launches can keep serving stale cached copies of
    # index.html/app.js/style.css from before a code update, causing DOM/JS
    # version-skew (e.g. app.js referencing an element index.html hasn't
    # added yet), which throws and can silently kill this app's boot
    # sequence with no visible error. The default per-launch temp profile
    # avoids that entirely at the cost of not reusing WebView2's cache.

    if sys.platform == "win32":
        # Force EdgeChromium backend — skip auto-detection which can cause
        # "not responding" hangs during startup on some Windows installs.
        start_kwargs["gui"] = "edgechromium"
        # Disabling GPU acceleration avoids hangs when WebView2's GPU process
        # stalls (older GPUs, VMs, remote desktop, flaky drivers). Use ONLY
        # --disable-gpu: it falls back to software rendering. Do NOT also pass
        # --disable-software-rasterizer, which removes that fallback and can
        # leave the window blank. setdefault() lets a user override the flags.
        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu",
        )
    # Set icon via start() (pywebview 5.x/6.x — icon is NOT a create_window param)
    if ICO_PATH.is_file():
        start_kwargs["icon"] = str(ICO_PATH.resolve())

    _startup_log(
        "calling webview.start  "
        f"gui={start_kwargs.get('gui', 'auto')}  "
        f"flags={os.environ.get('WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', '')!r}  "
        f"storage={start_kwargs.get('storage_path', '(default)')}"
    )

    try:
        webview.start(**start_kwargs)
        _startup_log("webview.start returned (window closed normally)")
    except Exception as e:
        _startup_log(f"webview.start raised {type(e).__name__}: {e}")
        _show_error("Make No Mistakes - webview failed",
                    f"{type(e).__name__}: {e}\n\n"
                    f"Make sure WebView2 is installed:\n"
                    f"https://developer.microsoft.com/en-us/microsoft-edge/webview2/\n\n"
                    f"Or run this in a terminal to see the full error:\n"
                    f"  python -m glmcode.gui --debug")


if __name__ == "__main__":
    import traceback
    try:
        main()
    except Exception:
        _show_error("Make No Mistakes crashed", traceback.format_exc())
