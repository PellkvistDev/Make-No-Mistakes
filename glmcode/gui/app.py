"""Make No Mistakes desktop app: pywebview window + JS bridge around the agent core."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
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
from ..config import PERMISSION_MODES, Config, load_config, save_config
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

    def reasoning_delta(self, text):
        self.emit("reasoning", text=text)

    def content_delta(self, text):
        self.emit("content", text=text)

    def stream_end(self):
        self.emit("stream_end")

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
        elif key in ("thinking", "show_reasoning"):
            setattr(c, key, bool(value))
        elif key in ("model", "vision_model") and isinstance(value, str) and value.strip():
            setattr(c, key, value.strip())
            if key == "model" and self.agent:
                self.agent.rebuild_system_prompt()
        elif key == "temperature":
            try:
                c.temperature = min(1.5, max(0.0, float(value)))
            except (TypeError, ValueError):
                pass
        else:
            return {"error": f"unknown setting {key}"}
        save_config(c)
        return self._settings()

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
        return {
            "ok": True, "id": sid, "cwd": str(Path.cwd()), "cwd_missing": not cwd_ok,
            "items": to_display(messages), "todos": get_todos(),
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
