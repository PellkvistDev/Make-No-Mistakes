"""A persistent, interactively-controllable browser for the agent.

Playwright's sync API is not thread-safe: every call must happen on the one
thread that created the Playwright object. Sub-agents, however, run on their
own worker threads and are discarded after each turn -- so the browser can't
live inside a sub-agent, or its state (cookies, login, current page) would die
with it. Instead a BrowserSession owns a dedicated DRIVER THREAD that creates
Playwright, launches Chromium, and processes commands off a queue. Any thread
can call the public methods; each one marshals a command onto the driver
thread and blocks for the result. The session lives at the chat level, so it
survives across many `control_chrome` delegations.

Perception is a numbered ACCESSIBILITY SNAPSHOT rather than pixels -- a
text-only model can read `[12] button "Sign in"` and act on ref 12, which is
far more reliable than guessing coordinates. A screenshot can still be taken
and routed through the vision model when the visual layout itself matters.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Callable, Optional

# Elements a user can actually interact with -- what the snapshot enumerates.
INTERACTIVE_SELECTOR = (
    "a, button, input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=textbox], [role=checkbox], "
    "[role=radio], [role=tab], [role=menuitem], [role=switch], [onclick]"
)

StatusFn = Optional[Callable[[str], None]]


class BrowserError(RuntimeError):
    """A browser action failed (bad ref, navigation error, closed session)."""


class BrowserSession:
    def __init__(self, *, headless: bool = False, viewport=(1280, 800),
                 executable_path: str | None = None, status: StatusFn = None,
                 launch_factory: Callable | None = None,
                 max_elements: int = 200):
        """launch_factory(headless, executable_path, viewport) -> (teardown, page)
        is called ON THE DRIVER THREAD to produce a Playwright page; the default
        uses real Playwright. Tests inject a fake to exercise all the routing,
        ref-tracking and snapshot logic without a real Chromium."""
        self.headless = headless
        self.viewport = viewport
        self.executable_path = executable_path
        self.status = status
        self._launch_factory = launch_factory or _real_launch
        self.max_elements = max_elements

        self._cmd_q: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._start_lock = threading.Lock()
        self._closed = False

        # Driver-thread-only state:
        self._page = None
        self._teardown: Callable | None = None
        self._refs: dict = {}

    # -- lifecycle -------------------------------------------------------- #

    def start(self) -> None:
        """Launch the browser (idempotent). Blocks until it's ready or raises
        the launch error. Safe to call from any thread."""
        with self._start_lock:
            if self._closed:
                raise BrowserError("This browser session has been closed.")
            if self._thread is not None:
                if self._start_error:
                    raise self._start_error
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._ready.wait()
        if self._start_error:
            raise self._start_error

    def close(self) -> None:
        """Tear the browser down and stop the driver thread. Idempotent."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        done = threading.Event()
        self._cmd_q.put(("__close__", {}, {}, done))
        done.wait(timeout=15)

    @property
    def is_open(self) -> bool:
        return self._thread is not None and not self._closed and self._start_error is None

    # -- driver thread ---------------------------------------------------- #

    def _run(self) -> None:
        try:
            self._teardown, self._page = self._launch_factory(
                self.headless, self.executable_path, self.viewport)
        except Exception as e:  # launch failed -- report it to start()
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            op, kw, box, done = self._cmd_q.get()
            if op == "__close__":
                try:
                    if self._teardown:
                        self._teardown()
                except Exception:
                    pass
                finally:
                    done.set()
                return
            try:
                box["result"] = self._dispatch(op, kw)
            except BrowserError as e:
                box["error"] = e
            except Exception as e:
                box["error"] = BrowserError(f"{type(e).__name__}: {e}")
            finally:
                done.set()

    def _call(self, op: str, **kw):
        """Marshal one command onto the driver thread and wait for its result."""
        if self._closed:
            raise BrowserError("This browser session has been closed.")
        self.start()
        box: dict = {}
        done = threading.Event()
        self._cmd_q.put((op, kw, box, done))
        if not done.wait(timeout=60):
            raise BrowserError(f"Browser command '{op}' timed out after 60s.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _dispatch(self, op: str, kw: dict):
        return getattr(self, "_op_" + op)(**kw)

    # -- public API (each marshals to the driver thread) ------------------ #

    def navigate(self, url: str) -> str:
        return self._call("navigate", url=url)

    def snapshot(self) -> str:
        return self._call("snapshot")

    def click(self, ref: int) -> str:
        return self._call("click", ref=ref)

    def type_text(self, ref: int, text: str, submit: bool = False) -> str:
        return self._call("type_text", ref=ref, text=text, submit=submit)

    def press(self, key: str) -> str:
        return self._call("press", key=key)

    def read_text(self, max_chars: int = 6000) -> str:
        return self._call("read_text", max_chars=max_chars)

    def screenshot(self, path) -> str:
        return self._call("screenshot", path=str(path))

    def go_back(self) -> str:
        return self._call("go_back")

    def current_url(self) -> str:
        return self._call("current_url")

    def screenshot_b64(self, max_width: int = 520) -> str:
        """A small JPEG data-URL of the current page, for the live Browser
        panel. Returns '' on any failure (best-effort live frame)."""
        return self._call("screenshot_b64", max_width=max_width)

    # -- operations (driver thread only) ---------------------------------- #

    def _op_navigate(self, url: str) -> str:
        url = (url or "").strip()
        if not url:
            raise BrowserError("navigate needs a url.")
        if not url.startswith(("http://", "https://", "about:", "file://", "data:")):
            url = "https://" + url
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            raise BrowserError(f"Could not load {url}: {e}")
        self._settle()
        return self._op_snapshot()

    def _op_snapshot(self) -> str:
        page = self._page
        try:
            handles = page.query_selector_all(INTERACTIVE_SELECTOR)
        except Exception as e:
            raise BrowserError(f"Could not read the page: {e}")
        self._refs = {}
        lines: list[str] = []
        i = 0
        for h in handles:
            try:
                if not h.is_visible():
                    continue
            except Exception:
                continue
            tag = self._tag(h)
            label = self._describe(h, tag)
            i += 1
            self._refs[i] = h
            lines.append(f"[{i}] {tag} {label}".rstrip())
            if i >= self.max_elements:
                break
        header = f"Page title: {self._title()}\nURL: {self._url()}\n"
        if not lines:
            return (header + "(No interactive elements detected. Use "
                    "browser_read to read the page's text content.)")
        return (header + f"Interactive elements ({len(lines)}):\n"
                + "\n".join(lines)
                + "\n\nClick with browser_click(ref), fill inputs with "
                  "browser_type(ref, text).")

    def _op_click(self, ref: int) -> str:
        h = self._ref(ref)
        try:
            h.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            h.click(timeout=10_000)
        except Exception as e:
            raise BrowserError(f"Could not click [{ref}]: {e}")
        self._settle()
        return self._op_snapshot()

    def _op_type_text(self, ref: int, text: str, submit: bool) -> str:
        h = self._ref(ref)
        try:
            h.fill(str(text), timeout=10_000)
            if submit:
                h.press("Enter")
        except Exception as e:
            raise BrowserError(f"Could not type into [{ref}]: {e}")
        self._settle()
        return self._op_snapshot()

    def _op_press(self, key: str) -> str:
        try:
            self._page.keyboard.press(key)
        except Exception as e:
            raise BrowserError(f"Could not press '{key}': {e}")
        self._settle()
        return self._op_snapshot()

    def _op_read_text(self, max_chars: int) -> str:
        try:
            txt = self._page.inner_text("body")
        except Exception as e:
            raise BrowserError(f"Could not read page text: {e}")
        txt = txt or ""
        if len(txt) > max_chars:
            txt = txt[:max_chars] + f"\n... [truncated, {len(txt)} chars total]"
        return f"URL: {self._url()}\n\n{txt}"

    def _op_screenshot(self, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._page.screenshot(path=str(p))
        except Exception as e:
            raise BrowserError(f"Could not screenshot: {e}")
        return str(p)

    def _op_go_back(self) -> str:
        try:
            self._page.go_back(wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            raise BrowserError(f"Could not go back: {e}")
        self._settle()
        return self._op_snapshot()

    def _op_current_url(self) -> str:
        return self._url()

    def _op_screenshot_b64(self, max_width: int) -> str:
        try:
            png = self._page.screenshot()
        except Exception:
            return ""
        try:
            import base64
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(png))
            if img.width > max_width:
                h = max(1, int(img.height * max_width / img.width))
                img = img.resize((max_width, h))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=70)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            import base64
            return "data:image/png;base64," + base64.b64encode(png).decode()

    # -- driver-thread helpers -------------------------------------------- #

    def _ref(self, ref):
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            raise BrowserError(f"Invalid element ref: {ref!r}")
        h = self._refs.get(ref)
        if h is None:
            raise BrowserError(
                f"No element [{ref}] in the current snapshot. Call "
                "browser_snapshot again -- refs change after every action.")
        return h

    def _settle(self) -> None:
        try:
            self._page.wait_for_timeout(600)
        except Exception:
            pass

    def _tag(self, h) -> str:
        try:
            return (h.evaluate("e => e.tagName") or "").lower()
        except Exception:
            return "?"

    def _describe(self, h, tag: str) -> str:
        for attr in ("aria-label", "placeholder", "name", "alt", "title", "value"):
            try:
                v = h.get_attribute(attr)
            except Exception:
                v = None
            if v and v.strip():
                return f'"{v.strip()[:90]}"'
        try:
            txt = (h.text_content() or "").strip()
        except Exception:
            txt = ""
        if txt:
            return f'"{" ".join(txt.split())[:90]}"'
        try:
            typ = h.get_attribute("type")
        except Exception:
            typ = None
        return f"({typ})" if typ else ""

    def _url(self) -> str:
        try:
            return self._page.url
        except Exception:
            return "?"

    def _title(self) -> str:
        try:
            return (self._page.title() or "").strip() or "(untitled)"
        except Exception:
            return "(untitled)"


def _real_launch(headless: bool, executable_path: str | None, viewport):
    """Default driver-thread launcher: ensure Playwright+Chromium are present,
    start Playwright, launch a browser, and return (teardown, page)."""
    from .browser import _install_packages, packages_installed, ready
    # A caller that supplied an explicit browser binary doesn't need (and
    # can't benefit from) the managed install/download -- just make sure the
    # playwright package itself is importable.
    if executable_path:
        if not packages_installed():
            _install_packages()
    elif not ready():
        _install_packages()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    kwargs = {"headless": headless}
    if executable_path:
        kwargs["executable_path"] = executable_path
    browser = pw.chromium.launch(**kwargs)
    context = browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]})
    page = context.new_page()

    def teardown():
        try:
            context.close()
        finally:
            try:
                browser.close()
            finally:
                pw.stop()

    return teardown, page
