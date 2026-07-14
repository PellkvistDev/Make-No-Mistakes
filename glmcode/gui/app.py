"""Make No Mistakes desktop app: pywebview window + JS bridge around the agent core."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
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


# --------------------------------------------------------------------- #

class WebEvents(AgentEvents):
    """Pushes agent events into the webview as JSON; blocks on permissions."""

    def __init__(self):
        self.window: webview.Window | None = None
        self._pending: dict[str, dict] = {}
        self.cfg = None  # set by Api.__init__ to the shared Config instance

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

    def emit(self, type_: str, **data) -> None:
        if not self.window:
            return
        payload = json.dumps({"type": type_, **data})
        try:
            # Hand the event to the page's sink. The payload is already
            # JSON-encoded, so it drops straight into the JS call.
            self.window.evaluate_js(
                f"window.GLM && window.GLM.emit({payload});"
            )
        except Exception:
            # A dropped UI update must never take down the agent turn.
            pass

    # streaming ---------------------------------------------------------
    def stream_start(self):
        self.emit("stream_start")
        self._tts_raw = ""
        self._tts_sent_len = 0
        self._tts_buffer = ""

    def reasoning_delta(self, text):
        self.emit("reasoning", text=text)

    def content_delta(self, text):
        self.emit("content", text=text)
        if self.read_aloud_this_turn:
            self._feed_tts(text)

    def stream_end(self):
        self.emit("stream_end")
        if self.read_aloud_this_turn and self._tts_buffer.strip():
            self._enqueue_tts_chunk(self._tts_buffer.strip())
            self._tts_buffer = ""

    # read-aloud ----------------------------------------------------------
    def start_turn(self, read_aloud: bool) -> None:
        """Called once per user turn (Api.send), before the agent runs."""
        self.read_aloud_this_turn = bool(read_aloud)
        self._tts_seq = 0
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

    def _pop_ready_tts_chunk(self, min_len: int = 40, max_len: int = 400) -> str | None:
        """Pull one complete, speakable chunk off the buffer once a sentence
        boundary is reached (so short abbreviations like "Mr." don't fire a
        tiny synthesis call on their own), with a safety valve that force-
        flushes at a word break if the buffer grows too long without one
        (e.g. unusual punctuation)."""
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
            voice = (self.cfg.tts_voice if self.cfg else None) or tts_mod.DEFAULT_VOICE
            speed = (self.cfg.tts_speed if self.cfg else None) or 1.0
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

    # sub-agents ----------------------------------------------------------
    def subagent(self, id, name, status, mission="", summary=""):
        self.emit("subagent", id=id, name=name, status=status,
                  mission=mission, summary=summary)

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
        self.cfg: Config = load_config()
        self.events = WebEvents()
        self.events.cfg = self.cfg  # shared reference: live settings changes apply immediately
        self.agent: Agent | None = None
        self.window: webview.Window | None = None
        self.store = SessionStore()
        self.session_id: str | None = None
        self.session_title: str = ""   # AI-chosen chat name; "" until first turn
        self._client: ZaiClient | None = None
        self._turn_lock = threading.Lock()

        configure_search(self.cfg.search_provider, self.cfg.resolve_tavily_key())
        # Initialize command aliases for npm/yarn/pnpm/git
        add_command_aliases({
            "npm": "npm",
            "yarn": "npm",
            "pnpm": "npm",
            "git": "git",
        })

    def _ensure_client(self) -> ZaiClient | None:
        key = self.cfg.resolve_api_key()
        if not key:
            return None
        if self._client is None:
            self._client = ZaiClient(key, self.cfg.base_url)
        return self._client

    def _fresh_agent(self) -> Agent | None:
        client = self._ensure_client()
        if not client:
            return None
        return Agent(self.cfg, client, events=self.events)

    # -- lifecycle ------------------------------------------------------- #

    def log(self, msg: str):
        """Let the page drop breadcrumbs into the startup log (see _startup_log).
        Lets us tell a native WebView2 hang (no JS ever runs) apart from a hang
        inside boot() (JS logged 'boot:start' but never 'boot:done')."""
        _startup_log(f"[js] {msg}")
        return {"ok": True}

    def boot(self):
        _startup_log("[py] boot() called")
        has_key = bool(self.cfg.resolve_api_key())
        result = {
            "version": __version__,
            "needsKey": not has_key,
            "background": self.get_background(),
            "settings": self._settings(),
            "sessions": self.list_sessions(),
            "session": None,
            "contextLimit": self.cfg.context_limit_tokens,
        }
        if has_key:
            result["session"] = self._resume_last()
            result["sessions"] = self.list_sessions()
        _startup_log("[py] boot() returning")
        return result

    def _resume_last(self):
        """Reopen the last active session on launch, if any still exists."""
        sid = self.cfg.last_session_id
        data = self.store.load(sid) if sid else None
        if data is None:
            sessions = self.list_sessions()
            if sessions:
                sid = sessions[0]["id"]
                data = self.store.load(sid)
        if data is None:
            self.agent = None
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
        w = self.window
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
        c = self.cfg
        return {
            "mode": c.mode, "model": c.model, "vision_model": c.vision_model,
            "vision_route": c.vision_route, "thinking": c.thinking,
            "show_reasoning": c.show_reasoning, "temperature": c.temperature,
            "cwd": str(Path.cwd()) if self.session_id else "",
            "background_custom": bool(c.background_path),
            "read_aloud": c.read_aloud, "tts_voice": c.tts_voice, "tts_speed": c.tts_speed,
        }

    def set_setting(self, key: str, value):
        c = self.cfg
        if key == "mode" and value in PERMISSION_MODES:
            if self.agent:
                self.agent.set_mode(value)
            else:
                c.mode = value
        elif key == "vision_route" and value in ("describe", "direct"):
            c.vision_route = value
        elif key in ("thinking", "show_reasoning", "read_aloud"):
            setattr(c, key, bool(value))
        elif key in ("model", "vision_model") and isinstance(value, str) and value.strip():
            setattr(c, key, value.strip())
            if key == "model" and self.agent:
                self.agent.rebuild_system_prompt()
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
                                 voice=voice, status=self.events.info)
            except Exception as e:
                return {"error": str(e)}
        try:
            return {"ok": True, "src": _data_uri(cache_path)}
        except OSError as e:
            return {"error": str(e)}

    # -- background ---------------------------------------------------------- #

    def get_background(self) -> str:
        p = Path(self.cfg.background_path) if self.cfg.background_path else None
        if p and p.is_file():
            try:
                return _data_uri(p)
            except OSError:
                pass
        return _data_uri(DEFAULT_BG)

    def pick_background(self):
        picked = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)",
                        "All files (*.*)"),
        )
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            return {"error": "not an image file"}
        self.cfg.background_path = str(path)
        save_config(self.cfg)
        return {"background": self.get_background()}

    def reset_background(self):
        self.cfg.background_path = ""
        save_config(self.cfg)
        return {"background": self.get_background()}

    # -- sessions (chat history + per-project work folder) ------------------ #

    def list_sessions(self):
        return self.store.list()

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
        self.agent = agent
        self.session_id = sid
        self.session_title = title
        restore_todos(todos)
        self.cfg.last_session_id = sid
        save_config(self.cfg)
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
        if self.agent and self.agent.busy:
            return {"error": "busy"}
        picked = self.window.create_file_dialog(webview.FOLDER_DIALOG)
        if not picked:
            return {"cancelled": True}
        path = Path(picked[0] if isinstance(picked, (list, tuple)) else picked)
        if not path.is_dir():
            return {"error": "not a folder"}
        res = self._activate_session(new_id(), [], str(path), 0, 0, [])
        res["sessions"] = self.list_sessions()
        return res

    def open_session(self, sid: str):
        if self.agent and self.agent.busy:
            return {"error": "busy"}
        data = self.store.load(sid)
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
        self.store.delete(sid)
        closed_active = sid == self.session_id
        if closed_active:
            self.agent = None
            self.session_id = None
            if self.cfg.last_session_id == sid:
                self.cfg.last_session_id = ""
                save_config(self.cfg)
        return {"ok": True, "sessions": self.list_sessions(), "closed_active": closed_active}

    def _save_current(self) -> None:
        if self.agent and self.session_id:
            u = self.agent.session_usage
            self.store.save(self.session_id, str(Path.cwd()), self.agent.messages,
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
                model=self.cfg.model,
                messages=[{"role": "user",
                           "content": TITLE_PROMPT.format(message=first_message[:2000])}],
                tools=None, temperature=0.3, max_tokens=24, thinking=False,
            )
            title = " ".join((res.content or "").split()).strip().strip('"\'').rstrip(".")
            return title[:64]
        except Exception:
            return ""

    # -- images ---------------------------------------------------------- #

    def pick_images(self):
        picked = self.window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("Images (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.gif)",),
        )
        if not picked:
            return []
        out = []
        for p in picked:
            path = Path(p)
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                out.append({"path": str(path), "name": path.name,
                            "thumb": _thumb_uri(path)})
        return out

    # -- chat ---------------------------------------------------------- #

    def send(self, text: str, image_paths: list | None = None):
        if not self.agent or not self.session_id:
            return {"error": "no active chat — start a New Chat first"}
        if not self._turn_lock.acquire(blocking=False):
            return {"error": "busy"}
        try:
            text = (text or "").strip()
            paths = [Path(p) for p in (image_paths or []) if Path(p).is_file()]
            if not text and not paths:
                return {"error": "empty"}
            if paths:
                msg = self.agent.attach_images(text, paths)
            else:
                msg = {"role": "user", "content": text}
            # Snapshot the read-aloud toggle for this turn only: if it's off
            # right now, TTS is never touched below, even if the user flips
            # it mid-response; if it's on, it stays on for this whole turn
            # regardless of later toggling.
            self.events.start_turn(self.cfg.read_aloud)
            self.agent.run_turn(msg)
            # First turn of a fresh chat: let the model name it for the sidebar.
            if not self.session_title and text:
                t = self._generate_title(text)
                if t:
                    self.session_title = t
            self._save_current()  # persist now so the returned sidebar is current
            u = self.agent.session_usage
            return {"ok": True, "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "context": self.agent.context_estimate(),
                    "title": self.session_title,
                    "sessions": self.list_sessions()}
        except Exception as e:
            self.events.error(f"{type(e).__name__}: {e}")
            return {"error": str(e)}
        finally:
            self._save_current()
            self._turn_lock.release()

    def cancel(self):
        if self.agent:
            self.agent.request_cancel()
        return {"ok": True}

    def permission_response(self, rid: str, answer: str, feedback: str = ""):
        self.events.resolve_permission(rid, answer, feedback)
        return {"ok": True}

    def clear_chat(self):
        """Start a fresh chat in the same project folder; the old conversation
        stays in history (nothing is discarded)."""
        if self.agent and self.agent.busy:
            return {"error": "busy"}
        if not self.session_id:
            return {"error": "no active chat"}
        cwd = str(Path.cwd())
        # Don't delete the old session — it stays in the sidebar as history
        res = self._activate_session(new_id(), [], cwd, 0, 0, [])
        res["sessions"] = self.list_sessions()  # refresh sidebar
        return res

    def compact_chat(self):
        if not self.agent or self.agent.busy:
            return {"error": "busy or not ready"}
        try:
            note = self.agent.compact()
            self._save_current()
            return {"ok": True, "note": note, "sessions": self.list_sessions(),
                    "context": self.agent.context_estimate()}
        except Exception as e:
            return {"error": str(e)}

    def usage(self):
        if not self.agent:
            return {"prompt_tokens": 0, "completion_tokens": 0, "context": 0}
        u = self.agent.session_usage
        return {"prompt_tokens": u.prompt_tokens,
                "completion_tokens": u.completion_tokens,
                "context": self.agent.context_estimate()}


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
    api.window = window
    api.events.window = window

    # Build webview.start() kwargs
    start_kwargs = dict(debug="--debug" in sys.argv)

    # Give WebView2 a stable, writable, ASCII profile directory instead of the
    # throwaway temp profile pywebview uses in private mode. A per-launch temp
    # profile in an odd path (non-ASCII username, OneDrive-synced folder,
    # locked-down temp dir) is the most common cause of *intermittent*
    # "not responding" hangs while the window comes up. A fixed folder under
    # ~/.glmcode also lets WebView2 reuse its cache across launches.
    try:
        storage = Path.home() / ".glmcode" / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        start_kwargs["storage_path"] = str(storage)
        start_kwargs["private_mode"] = False
    except OSError:
        pass  # fall back to pywebview defaults if the folder can't be made

    if sys.platform == "win32":
        # Force EdgeChromium backend — skip auto-detection which can cause
        # "not responding" hangs during startup on some Windows installs.
        start_kwargs["gui"] = "edgechromium"
        # Disabling GPU acceleration avoids hangs when WebView2's GPU process
        # stalls (older GPUs, VMs, remote desktop, flaky drivers). Use ONLY
        # --disable-gpu: it falls back to software rendering. Do NOT also pass
        # --disable-software-rasterizer, which removes that fallback and can
        # leave the window blank.
        # --disable-renderer-accessibility works around a real pywebview/
        # pythonnet bug (r0x0r/pywebview#1815): when something walks the
        # WinForms host window's UI Automation tree — which WebView2's own
        # accessibility bridge can trigger on its own, no screen reader
        # needed — pythonnet's proxy for System.Drawing.Rectangle.Empty on
        # Form.AccessibilityObject.Bounds recurses infinitely. The Python
        # exception gets caught and logged ("Error while processing
        # events...maximum recursion depth exceeded"), but it happens on the
        # UI thread mid-message-pump and the window never responds again
        # afterward. This flag stops WebView2's content from registering
        # that accessibility bridge in the first place, at the cost of
        # screen readers not being able to read the page content.
        # setdefault() lets a user override the flags.
        os.environ.setdefault(
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
            "--disable-gpu --disable-renderer-accessibility",
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
