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
from ..config import CONFIG_DIR, PERMISSION_MODES, Config, load_config, save_config
from ..events import AgentEvents
from ..prompts import TITLE_PROMPT
from ..sessions import SessionStore, new_id, to_display
from ..tools import configure_search, get_todos, restore_todos
from ..permissions import add_command_aliases

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_BG = WEB_DIR / "bg-default.jpg"
# Always-available scratch folder for quick, throwaway projects -- a sibling
# of this app's own install directory (e.g. .../Theo/Make No Mistakes ->
# .../Theo/whiteboard), created on first use rather than at import time.
WHITEBOARD_DIR = Path(__file__).resolve().parents[3] / "whiteboard"


# --------------------------------------------------------------------- #

class WebEvents(AgentEvents):
    """Pushes agent events into the webview as JSON; blocks on permissions."""

    def __init__(self):
        # Underscore-prefixed: pywebview's inject_pywebview() recursively
        # introspects every non-underscore attribute of the js_api object to
        # build the exposed JS surface. A public `window` attribute gets
        # walked into window.native (the WinForms Form), whose
        # AccessibilityObject.Bounds.Empty chain recurses infinitely in
        # pythonnet (Rectangle.Empty returns another Rectangle exposing its
        # own .Empty). That blows the window's UI thread and freezes the
        # app permanently. Leading underscore makes pywebview skip it.
        self._window: webview.Window | None = None
        self._pending: dict[str, dict] = {}
        self._cfg = None  # set by Api.__init__ to the shared Config instance

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
        self._flush_thread_started = False

    def emit(self, type_: str, **data) -> None:
        if not self._window:
            return
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
        if reasoning:
            self.emit("reasoning", text=reasoning)
        if content:
            self.emit("content", text=content)

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

    # sub-agents ----------------------------------------------------------
    def subagent(self, id, name, status, mission="", summary=""):
        self.emit("subagent", id=id, name=name, status=status,
                  mission=mission, summary=summary)

    def subagent_stream(self, id, kind, **data):
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

class Api:
    """Methods callable from JS via window.pywebview.api.*"""

    def __init__(self):
        self._cfg: Config = load_config()
        self._events = WebEvents()
        self._events._cfg = self._cfg  # shared reference: live settings changes apply immediately
        self._agent: Agent | None = None
        # Underscore-prefixed: see the comment on WebEvents._window above —
        # this class is the js_api object pywebview recursively introspects,
        # so a public `window` attribute here triggers the same infinite
        # AccessibilityObject.Bounds.Empty recursion and freezes the app.
        self._window: webview.Window | None = None
        self._store = SessionStore()
        self.session_id: str | None = None
        self.session_title: str = ""   # AI-chosen chat name; "" until first turn
        self._client: ZaiClient | None = None
        self._turn_lock = threading.Lock()

        configure_search(self._cfg.search_provider, self._cfg.resolve_tavily_key())
        # Initialize command aliases for npm/yarn/pnpm/git
        add_command_aliases({
            "npm": "npm",
            "yarn": "npm",
            "pnpm": "npm",
            "git": "git",
        })

    def _ensure_client(self) -> ZaiClient | None:
        key = self._cfg.resolve_api_key()
        if not key:
            return None
        if self._client is None:
            self._client = ZaiClient(key, self._cfg.base_url)
        return self._client

    def _fresh_agent(self) -> Agent | None:
        client = self._ensure_client()
        if not client:
            return None
        return Agent(self._cfg, client, events=self._events)

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
        elif key in ("thinking", "show_reasoning", "read_aloud"):
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

    def _activate_session(self, sid: str, messages: list, cwd: str,
                          prompt_tokens: int, completion_tokens: int,
                          todos: list, title: str = "") -> dict:
        cwd_ok = True
        if cwd:
            try:
                os.chdir(cwd)
            except OSError:
                cwd_ok = False
        agent = self._fresh_agent()
        if agent is None:
            return {"error": "no API key configured"}
        agent.load_messages(messages)
        agent.set_usage(prompt_tokens, completion_tokens)
        self._agent = agent
        self.session_id = sid
        self.session_title = title
        restore_todos(todos)
        self._cfg.last_session_id = sid
        save_config(self._cfg)
        # cwd is already switched above, so relative image/audio paths saved
        # by generate_image/show_image/speak resolve correctly here.
        items = to_display(messages)
        for it in items:
            if it.get("kind") in ("tool_image", "tool_audio") and it.get("path"):
                try:
                    it["src"] = _data_uri(Path(it["path"]))
                except OSError:
                    it["src"] = ""  # file moved/deleted since it was shown
        return {
            "ok": True, "id": sid, "cwd": str(Path.cwd()), "cwd_missing": not cwd_ok,
            "items": items, "todos": get_todos(),
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "context": agent.context_estimate(),
        }

    def new_session(self):
        """Start a brand-new chat. The user picks the project folder themselves —
        nothing is auto-created or defaulted."""
        if self._agent and self._agent.busy:
            return {"error": "busy"}
        picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_dir():
            return {"error": "not a folder"}
        res = self._activate_session(new_id(), [], str(path), 0, 0, [])
        res["sessions"] = self.list_sessions()
        return res

    def open_whiteboard(self):
        """Start a brand-new chat in the always-available scratch folder,
        creating it next to this app's own install directory if this is the
        first time it's used. No folder picker -- unlike new_session, there's
        nothing to choose."""
        if self._agent and self._agent.busy:
            return {"error": "busy"}
        WHITEBOARD_DIR.mkdir(parents=True, exist_ok=True)
        res = self._activate_session(new_id(), [], str(WHITEBOARD_DIR), 0, 0, [])
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
        if self._agent and self._agent.busy:
            return {"error": "busy"}
        data = self._store.load(sid)
        if not data:
            return {"error": "session not found"}
        res = self._activate_session(
            sid, data.get("messages", []), data.get("cwd", ""),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("todos", []), data.get("title", ""),
        )
        res["sessions"] = self.list_sessions()
        return res

    def delete_session(self, sid: str):
        self._store.delete(sid)
        closed_active = sid == self.session_id
        if closed_active:
            self._agent = None
            self.session_id = None
            if self._cfg.last_session_id == sid:
                self._cfg.last_session_id = ""
                save_config(self._cfg)
        return {"ok": True, "sessions": self.list_sessions(), "closed_active": closed_active}

    def _save_current(self) -> None:
        if self._agent and self.session_id:
            u = self._agent.session_usage
            self._store.save(self.session_id, str(Path.cwd()), self._agent.messages,
                            u.prompt_tokens, u.completion_tokens, todos=get_todos(),
                            title=self.session_title)

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

    def send(self, text: str, file_paths: list | None = None):
        if not self._agent or not self.session_id:
            return {"error": "no active chat — start a New Chat first"}
        if not self._turn_lock.acquire(blocking=False):
            return {"error": "busy"}
        try:
            text = (text or "").strip()
            paths = [Path(p) for p in (file_paths or []) if Path(p).is_file()]
            if not text and not paths:
                return {"error": "empty"}
            if paths:
                msg = self._agent.attach_files(text, paths)
            else:
                msg = {"role": "user", "content": text}
            # Snapshot the read-aloud toggle for this turn only: if it's off
            # right now, TTS is never touched below, even if the user flips
            # it mid-response; if it's on, it stays on for this whole turn
            # regardless of later toggling.
            self._events.start_turn(self._cfg.read_aloud)
            self._agent.run_turn(msg)
            # First turn of a fresh chat: let the model name it for the sidebar.
            if not self.session_title and text:
                t = self._generate_title(text)
                if t:
                    self.session_title = t
            self._save_current()  # persist now so the returned sidebar is current
            u = self._agent.session_usage
            return {"ok": True, "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "context": self._agent.context_estimate(),
                    "title": self.session_title,
                    "sessions": self.list_sessions()}
        except Exception as e:
            self._events.error(f"{type(e).__name__}: {e}")
            return {"error": str(e)}
        finally:
            self._save_current()
            self._turn_lock.release()

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
        cwd = str(Path.cwd())
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
            (Path.home() / ".glmcode" / "crash.log").write_text(
                f"{title}\n\n{message}", encoding="utf-8"
            )
        except OSError:
            pass


GUI_DIR = Path(__file__).parent          # glmcode/gui/
ICO_PATH = GUI_DIR / "app_icon.ico"     # pre-built, ships with package

STARTUP_LOG = Path.home() / ".glmcode" / "startup.log"


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
    api._events._window = window

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
