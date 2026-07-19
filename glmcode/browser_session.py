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

# One JS pass builds the whole snapshot: enumerates visible interactive
# elements, STAMPS each with a persistent data-mnm-ref (so refs stay stable
# across snapshots while the page lives -- the #1 cause of wrong-element
# clicks was renumbering on every snapshot), and reports label/region/state
# per element plus the page's heading outline. A single evaluate instead of
# 4-5 round trips per element also makes snapshots much faster.
SNAPSHOT_JS = """(sel) => {
  const regionOf = (e) => {
    if (e.closest('[role=dialog],[aria-modal="true"],dialog')) return 'dialog';
    if (e.closest('nav,[role=navigation]')) return 'nav';
    if (e.closest('header,[role=banner]')) return 'header';
    if (e.closest('footer,[role=contentinfo]')) return 'footer';
    return 'main';
  };
  const labelOf = (e) => {
    const cand = e.getAttribute('aria-label') || e.getAttribute('placeholder')
      || e.getAttribute('name') || e.getAttribute('alt') || e.getAttribute('title');
    if (cand && cand.trim()) return cand.trim();
    const t = (e.innerText || e.textContent || '').trim().replace(/\\s+/g, ' ');
    if (t) return t;
    if (typeof e.value === 'string' && e.value.trim()) return e.value.trim();
    return '';
  };
  let next = window.__mnmNextRef || 1;
  const seen = new Set();
  const out = [];
  for (const e of document.querySelectorAll(sel)) {
    const cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    let ref = parseInt(e.dataset.mnmRef || '', 10);
    if (!ref || seen.has(ref)) { ref = next++; e.dataset.mnmRef = String(ref); }
    seen.add(ref);
    const tag = e.tagName.toLowerCase();
    const item = {
      ref, tag,
      label: labelOf(e).slice(0, 80),
      region: regionOf(e),
      disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
      type: (e.getAttribute('type') || '').toLowerCase(),
    };
    if ((tag === 'input' || tag === 'textarea') && typeof e.value === 'string'
        && e.value && item.type !== 'submit' && item.type !== 'button')
      item.value = e.value.slice(0, 40);
    if (tag === 'select') {
      item.options = [...e.options].slice(0, 12).map(o => (o.label || o.value || '').slice(0, 40));
      const so = e.selectedOptions && e.selectedOptions[0];
      if (so) item.value = (so.label || so.value || '').slice(0, 40);
    }
    if (e.checked === true) item.checked = true;
    out.push(item);
  }
  window.__mnmNextRef = next;
  const outline = [...document.querySelectorAll('h1,h2')].slice(0, 6)
    .map(h => (h.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 60))
    .filter(Boolean);
  return { items: out, outline };
}"""

# Region presentation order + captions. Dialogs first: a cookie banner or
# modal blocks everything else, so the model must see and handle it first.
_REGION_ORDER = {"dialog": 0, "main": 1, "header": 2, "nav": 3, "footer": 4}
_REGION_TITLES = {
    "dialog": "OPEN DIALOG / POPUP — deal with this first (accept, close or "
              "dismiss it); the page behind it is blocked:",
    "main": "Main content:",
    "header": "Header:",
    "nav": "Navigation:",
    "footer": "Footer:",
}
# Chrome regions are usually noise for the task at hand -- cap them.
_REGION_CAPS = {"header": 20, "nav": 20, "footer": 12}

StatusFn = Optional[Callable[[str], None]]


class BrowserError(RuntimeError):
    """A browser action failed (bad ref, navigation error, closed session)."""


class BrowserSession:
    def __init__(self, *, headless: bool = False, viewport=(1280, 800),
                 executable_path: str | None = None, status: StatusFn = None,
                 launch_factory: Callable | None = None,
                 max_elements: int = 200, user_data_dir: str | None = None):
        """launch_factory(headless, executable_path, viewport, user_data_dir)
        -> (teardown, page) is called ON THE DRIVER THREAD to produce a
        Playwright page; the default uses real Playwright. Tests inject a fake
        to exercise all the routing, ref-tracking and snapshot logic without a
        real Chromium.

        user_data_dir, when set, launches a PERSISTENT context rooted at that
        directory: cookies and logins survive across sessions and app
        restarts. It's a dedicated agent profile (never the user's own
        browser); the user logs into chosen sites once and the agent reuses
        them. None (the default) keeps the fully throwaway profile."""
        self.headless = headless
        self.viewport = viewport
        self.executable_path = executable_path
        self.status = status
        self._launch_factory = launch_factory or _real_launch
        self.max_elements = max_elements
        self.user_data_dir = user_data_dir

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
                self.headless, self.executable_path, self.viewport,
                self.user_data_dir)
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

    def click_at(self, x: float, y: float) -> str:
        return self._call("click_at", x=x, y=y)

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

    def wait(self, seconds: float = 2.0) -> str:
        return self._call("wait", seconds=seconds)

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
        try:
            data = self._page.evaluate(SNAPSHOT_JS, INTERACTIVE_SELECTOR) or {}
        except Exception as e:
            raise BrowserError(f"Could not read the page: {e}")
        items = data.get("items") or []
        outline = data.get("outline") or []
        self._refs = {int(it["ref"]): it for it in items}

        header = (f"Page title: {self._title()}\nURL: {self._url()}\n"
                 f"Viewport: {self.viewport[0]}x{self.viewport[1]} px "
                 "(top-left is 0,0 -- for browser_click_at)\n")
        if outline:
            header += "Page sections: " + " | ".join(outline) + "\n"
        if not items:
            return (header + "(No interactive elements detected. Use "
                    "browser_read to read the page's text content.)")

        # How many share a (tag, label): duplicates get flagged so the model
        # knows "Add to cart" isn't unique and double-checks which one.
        from collections import Counter
        counts = Counter((it["tag"], it["label"]) for it in items)

        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(it.get("region") or "main", []).append(it)

        lines: list[str] = []
        total = 0
        for region in sorted(groups, key=lambda r: _REGION_ORDER.get(r, 9)):
            its = groups[region]
            cap = min(_REGION_CAPS.get(region, self.max_elements),
                      max(0, self.max_elements - total))
            lines.append(_REGION_TITLES.get(region, region + ":"))
            shown = 0
            for it in its:
                if shown >= cap:
                    lines.append(f"  (+{len(its) - shown} more {region} "
                                 "elements not shown)")
                    break
                shown += 1
                total += 1
                lines.append("  " + self._fmt_item(it, counts))
        return (header + "\n".join(lines)
                + "\n\nActs: browser_click(ref); browser_type(ref, text) for "
                  "inputs -- for a select, type the option text to choose it; "
                  "browser_click for checkboxes/radios. Elements marked "
                  "(disabled) can't be used until something enables them. "
                  "Refs stay stable on this page; new elements get new numbers. "
                  "If something isn't listed here (canvas-drawn UI, an SVG shape, "
                  "a spot on an image/map), use browser_screenshot to see it and "
                  "browser_click_at(x, y) to click its pixel position instead.")

    @staticmethod
    def _fmt_item(it: dict, counts) -> str:
        lab = f' "{it["label"]}"' if it.get("label") else (
            f' ({it["type"]})' if it.get("type") else "")
        s = f'[{it["ref"]}] {it["tag"]}{lab}'
        if it.get("value"):
            s += f' = "{it["value"]}"'
        if it.get("checked"):
            s += " (checked)"
        if it.get("options"):
            s += " (options: " + ", ".join(it["options"]) + ")"
        if it.get("disabled"):
            s += " (disabled)"
        n = counts[(it["tag"], it.get("label"))]
        if n > 1 and it.get("label"):
            s += f" (one of {n} with this label)"
        return s

    def _op_click(self, ref: int) -> str:
        h, it = self._locate(ref, "click")
        try:
            h.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        try:
            h.click(timeout=8_000)
        except Exception as e:
            raise BrowserError(self._action_failure("click", ref, e))
        self._settle()
        return self._op_snapshot()

    def _op_click_at(self, x, y) -> str:
        """Raw mouse click at viewport pixel coordinates -- the fallback for
        anything browser_click can't reach: canvas-drawn UI, SVG shapes, a
        spot on an image/map, or an element the accessibility scan simply
        missed. Prefer browser_click(ref) whenever the element IS in the
        snapshot; a ref click self-verifies (it targets a real element) in a
        way a raw coordinate never can."""
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            raise BrowserError(f"click_at needs numeric x, y (got {x!r}, {y!r}).")
        vw, vh = self.viewport
        if not (0 <= x <= vw and 0 <= y <= vh):
            raise BrowserError(
                f"({x:.0f}, {y:.0f}) is outside the {vw}x{vh} viewport -- "
                "coordinates must be within the visible page area shown at "
                "the top of every snapshot.")
        try:
            self._page.mouse.click(x, y)
        except Exception as e:
            raise BrowserError(
                f"Could not click at ({x:.0f}, {y:.0f}): {str(e).splitlines()[0]}")
        self._settle()
        return self._op_snapshot()

    def _op_type_text(self, ref: int, text: str, submit: bool) -> str:
        h, it = self._locate(ref, "type into")
        text = str(text)
        if it.get("tag") == "select":
            # A <select> can't be fill()ed -- choose the option instead. The
            # snapshot listed its options, so `text` should be one of them.
            try:
                h.select_option(label=text)
            except Exception:
                try:
                    h.select_option(value=text)
                except Exception:
                    opts = ", ".join(it.get("options") or []) or "(none seen)"
                    raise BrowserError(
                        f"[{ref}] is a dropdown and has no option '{text}'. "
                        f"Its options are: {opts}. browser_type the exact "
                        "option text to choose it.")
            self._settle()
            return self._op_snapshot()
        if it.get("type") in ("checkbox", "radio"):
            raise BrowserError(
                f"[{ref}] is a {it['type']} -- use browser_click({ref}) to "
                "toggle it, not typing.")
        try:
            h.fill(text, timeout=8_000)
            if submit:
                h.press("Enter")
        except Exception as e:
            raise BrowserError(self._action_failure("type into", ref, e))
        self._settle()
        return self._op_snapshot()

    # -- action pre-flight (driver thread) --------------------------------- #

    def _locate(self, ref, verb: str):
        """Resolve a ref to a FRESH element handle at action time (via its
        data-mnm-ref stamp), so we never act through a stale handle. Fails
        instantly with an actionable message when the ref is unknown, the
        element left the page, or it's disabled -- instead of letting
        Playwright spin its multi-second retry loop."""
        try:
            ref = int(ref)
        except (TypeError, ValueError):
            raise BrowserError(f"Invalid element ref: {ref!r}")
        it = self._refs.get(ref)
        if it is None:
            raise BrowserError(
                f"No element [{ref}] in the current snapshot. Call "
                "browser_snapshot and use the refs it shows.")
        h = None
        try:
            h = self._page.query_selector(f'[data-mnm-ref="{ref}"]')
        except Exception:
            pass
        if h is None:
            raise BrowserError(
                f"Element [{ref}] is no longer on the page -- it changed since "
                "your snapshot. Call browser_snapshot and use the fresh refs.")
        try:
            if not h.is_enabled():
                raise BrowserError(
                    f"Element [{ref}] is disabled (greyed out) right now, so "
                    f"you can't {verb} it. Something else likely has to happen "
                    "first (fill a required field, pick an option, wait for the "
                    "page). Look at the snapshot and do that step instead.")
        except BrowserError:
            raise
        except Exception:
            pass  # enabled-check itself failed -> let the action try
        return h, it

    @staticmethod
    def _action_failure(verb: str, ref, e) -> str:
        msg = str(e).split("\n")[0]  # first line; the Call log is pure noise
        return (f"Could not {verb} [{ref}]: {msg} -- the element may be "
                "covered by an overlay/dialog, or the page changed. Call "
                "browser_snapshot to re-orient (maybe close any popup first).")

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

    def _op_wait(self, seconds: float) -> str:
        try:
            seconds = max(0.2, min(float(seconds or 2.0), 10.0))
        except (TypeError, ValueError):
            seconds = 2.0
        try:
            self._page.wait_for_timeout(seconds * 1000)
        except Exception:
            pass
        return self._op_snapshot()

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

    def _settle(self) -> None:
        try:
            self._page.wait_for_timeout(600)
        except Exception:
            pass

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


def _real_launch(headless: bool, executable_path: str | None, viewport,
                 user_data_dir: str | None = None):
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
    vp = {"width": viewport[0], "height": viewport[1]}

    if user_data_dir:
        # Persistent profile: cookies/logins live in user_data_dir and
        # survive restarts. Chromium locks the dir, so a second concurrent
        # session on the same profile fails -- surface that clearly.
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir, viewport=vp, **kwargs)
        except Exception as e:
            pw.stop()
            if "ProcessSingleton" in str(e) or "user data directory is already in use" in str(e).lower():
                raise BrowserError(
                    "The saved browser profile is already in use -- another "
                    "chat's browser is open with it. Close that browser (or "
                    "chat) first, or turn off 'Remember browser logins'.")
            raise
        page = context.pages[0] if context.pages else context.new_page()

        def teardown():
            try:
                context.close()
            finally:
                pw.stop()

        return teardown, page

    browser = pw.chromium.launch(**kwargs)
    context = browser.new_context(viewport=vp)
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
