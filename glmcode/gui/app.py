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
from ..notify import APP_NAME, notify
from ..prompts import EXECUTE_PLAN_MESSAGE, PLAN_MODE_PREAMBLE, TITLE_PROMPT
from ..sessions import SessionStore, new_id, to_display
from ..transcript import Transcript, search_sessions
from ..tools import configure_search
from ..permissions import add_command_aliases

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_BG = WEB_DIR / "bg-default.jpg"
# Always-available scratch folder for quick, throwaway projects -- a sibling
# of this app's own install directory (e.g. .../Theo/Make No Mistakes ->
# .../Theo/whiteboard), created on first use rather than at import time.
WHITEBOARD_DIR = Path(__file__).resolve().parents[3] / "whiteboard"


# --------------------------------------------------------------------- #

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
        self._tts_raw = ""        # cumulative raw text this stream segment (fence tracking)
        self._tts_sent_len = 0    # how much of the fence-filtered prose is already buffered
        self._tts_buffer = ""     # buffered prose not yet synthesized
        self._tts_queue: "queue.Queue" = queue.Queue()
        self._tts_worker_started = False
        self._tts_seq = 0
        self._tts_first_chunk_done = False
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
        self._tts_raw = ""
        self._tts_sent_len = 0
        self._tts_buffer = ""

    def reasoning_delta(self, text):
        with self._stream_lock:
            self._reasoning_buf += text
        self._ensure_flush_thread()

    def content_delta(self, text):
        with self._stream_lock:
            self._content_buf += text
        self._ensure_flush_thread()
        if self.read_aloud_this_turn:
            self._feed_tts(text)

    def stream_end(self):
        self._flush_stream_buffers()  # make sure everything is sent before stream_end
        self.emit("stream_end")
        if self.read_aloud_this_turn and self._tts_buffer.strip():
            self._enqueue_tts_chunk(self._tts_buffer.strip())
            self._tts_buffer = ""

    # read-aloud ----------------------------------------------------------
    def start_turn(self, read_aloud: bool) -> None:
        """Called once per user turn (Api.send), before the agent runs."""
        self.read_aloud_this_turn = bool(read_aloud)
        self._tts_seq = 0
        self._tts_first_chunk_done = False
        self.emit("tts_reset")

    def _feed_tts(self, text: str) -> None:
        from ..tts import strip_code_fences_incremental
        self._tts_raw += text
        prose = strip_code_fences_incremental(self._tts_raw)
        new_prose = prose[self._tts_sent_len:]
        self._tts_sent_len = len(prose)
        if not new_prose:
            return
        self._tts_buffer += new_prose
        chunk = self._pop_ready_tts_chunk()
        while chunk:
            self._enqueue_tts_chunk(chunk)
            chunk = self._pop_ready_tts_chunk()

    _SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?=\s|$)")
    # The very first chunk of a turn uses a much lower min_len than later
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

    def _pop_ready_tts_chunk(self, min_len: int = 40, max_len: int = 400) -> str | None:
        """Pull one complete, speakable chunk off the buffer once a sentence
        boundary is reached (so short abbreviations like "Mr." don't fire a
        tiny synthesis call on their own), with a safety valve that force-
        flushes at a word break if the buffer grows too long without one
        (e.g. unusual punctuation)."""
        if not self._tts_first_chunk_done:
            min_len = self._FIRST_CHUNK_MIN_LEN
        buf = self._tts_buffer
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
        self._tts_buffer = buf[last_boundary:]
        self._tts_first_chunk_done = True
        return chunk or None

    def _enqueue_tts_chunk(self, text: str) -> None:
        if not text.strip():
            return
        self._ensure_tts_worker()
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
        from .. import tts as tts_mod
        while True:
            seq, text = self._tts_queue.get()
            voice = (self._cfg.tts_voice if self._cfg else None) or tts_mod.DEFAULT_VOICE
            speed = (self._cfg.tts_speed if self._cfg else None) or 1.0
            try:
                audio, sr = tts_mod.synthesize(text, voice=voice, speed=speed)
                wav = tts_mod.audio_to_wav_bytes(audio, sr)
                src = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
                self.emit("play_audio", seq=seq, src=src)
            except Exception as e:
                self.emit("play_audio", seq=seq, src="", error=str(e))

    # tools --------------------------------------------------------------
    def tool_call(self, name, args):
        self.emit("tool_call", name=name, args=args)

    def tool_result(self, name, content, is_error=False):
        self.emit("tool_result", name=name, content=content[:12000], error=is_error)

    def todos(self, items):
        self.emit("todos", items=items)

    # notices --------------------------------------------------------------
    def info(self, msg):
        self.emit("notice", level="info", text=msg)

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
    def compacted(self, summary):
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
            return
        # Everything else is rare but must stay ordered relative to the text
        # that streamed before it.
        self._flush_one_subagent(id)
        if kind == "tool_result":
            # Match the main chat's display cap -- the model already got the
            # full content; shipping up to 60KB per blocking IPC call to the
            # UI just to fill a collapsed chip was pure waste.
            data = dict(data, content=str(data.get("content", ""))[:12000])
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
    from ..tools import NO_WINDOW_KWARGS
    os.environ[name] = value
    try:
        r = subprocess.run(["setx", name, value], capture_output=True, timeout=15,
                           **NO_WINDOW_KWARGS)
        return r.returncode == 0
    except OSError:
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
        # Initialize command aliases for npm/yarn/pnpm/git
        add_command_aliases({
            "npm": "npm",
            "yarn": "npm",
            "pnpm": "npm",
            "git": "git",
        })

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
        return ev

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
        persisted = persist_env_var("ZAI_API_KEY", key)
        self._client = None
        session = self._resume_last()
        return {"ok": True, "persisted": persisted, "session": session,
                "sessions": self.list_sessions()}

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
            "show_reasoning": c.show_reasoning, "temperature": c.temperature,
            "cwd": str(Path.cwd()) if self.session_id else "",
            "background_custom": bool(c.background_path),
            "read_aloud": c.read_aloud, "tts_voice": c.tts_voice, "tts_speed": c.tts_speed,
            "notifications": c.notifications,
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
        elif key in ("thinking", "show_reasoning", "read_aloud", "notifications"):
            setattr(c, key, bool(value))
        elif key in ("model", "vision_model") and isinstance(value, str) and value.strip():
            setattr(c, key, value.strip())
            if key == "model" and self._agent:
                self._agent.rebuild_system_prompt()
        elif key == "tts_voice" and isinstance(value, str) and value.strip():
            c.tts_voice = value.strip()
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
        else:
            return {"error": f"unknown setting {key}"}
        save_config(c)
        return self._settings()

    # -- text-to-speech -------------------------------------------------------- #

    def tts_status(self):
        from ..tts import ready
        return {"ready": ready()}

    def tts_voices(self):
        from .. import tts as tts_mod
        return {"voices": tts_mod.list_voices()}

    def preview_voice(self, voice: str):
        """Synthesize (once, then cached on disk) and return a short sample
        of the given voice so Settings can offer an audition button. Can
        take a while on the very first call ever (full first-use install +
        download), same as any other first TTS use."""
        from .. import tts as tts_mod
        voice = (voice or "").strip() or tts_mod.DEFAULT_VOICE
        cache_dir = CONFIG_DIR / "models" / "kokoro" / "previews"
        cache_path = cache_dir / f"{voice}.wav"
        if not cache_path.is_file():
            try:
                tts_mod.save_wav(f"Hi, this is the {voice} voice.", cache_path,
                                 voice=voice, status=self._events.info)
            except Exception as e:
                return {"error": str(e)}
        try:
            return {"ok": True, "src": _data_uri(cache_path)}
        except OSError as e:
            return {"error": str(e)}

    # -- background ---------------------------------------------------------- #

    def get_background(self) -> str:
        p = Path(self._cfg.background_path) if self._cfg.background_path else None
        if p and p.is_file():
            try:
                return _data_uri(p)
            except OSError:
                pass
        return _data_uri(DEFAULT_BG)

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
                          model_provider: str = "", model: str = "") -> dict:
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
        agent.load_messages(messages)
        agent.set_usage(prompt_tokens, completion_tokens)
        agent.todos = list(todos or [])
        self._chats[sid] = ChatState(sid, agent, events)
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
        }

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
        )
        res["sessions"] = self.list_sessions()
        return res

    def delete_session(self, sid: str):
        live = self._chats.get(sid)
        if live and live.agent.busy:
            return {"error": "that chat is still working — stop it first"}
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
                         model_provider=cs.provider, model=cs.model)

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
        raw_text = text  # pre-wrap, for title generation
        ok = False
        try:
            events.emit("chat_busy")
            if plan and text:
                # Read-only planning turn: the preamble sets expectations and
                # permissions.plan_only (below) makes them non-negotiable.
                text = PLAN_MODE_PREAMBLE.format(text=text)
            msg = (agent.attach_files(text, paths) if paths
                   else {"role": "user", "content": text})
            agent.permissions.plan_only = bool(plan and text)
            # File backup: commit the project's current state (i.e. how it
            # looked right before this message's own edits) so "revert to
            # here" later can put it back. Best-effort -- a backup failure
            # must never block sending a message.
            if cs.auto_backup and cs.backup_repo:
                try:
                    # Visible in the status chip: on a big project the git
                    # snapshot can take a moment, and silent pre-turn latency
                    # reads as "the app is slow" rather than "it's working".
                    with events.status("backing up project files..."):
                        cs.backup_repo.snapshot(text or "(files attached)")
                except Exception as e:
                    events.warn(f"backup snapshot failed: {e}")
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
            self._os_attention(cs.sid, "Done -- waiting for you."
                               if ok else "Stopped on an error -- waiting for you.")

    def execute_plan(self):
        """The 'Execute plan' button: a normal (non-plan) turn with a canned
        instruction to carry out the plan the user just approved."""
        return self.send(EXECUTE_PLAN_MESSAGE)

    def cancel(self):
        if self._agent:
            self._agent.request_cancel()
        return {"ok": True}

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
