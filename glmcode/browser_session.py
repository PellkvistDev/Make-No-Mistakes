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

There are two ways to get a page, and they are not variations of one thing:

  - LAUNCH (the default, unchanged): this app starts its own Chromium, in its
    own throwaway or dedicated-agent profile. Nothing the agent does can touch
    the browser the user is signed into.
  - ATTACH (`connect_url`): the user is already running a browser with the
    DevTools port open, and the agent drives the window they are looking at,
    in their own session, with their own logins.

Attaching is strictly more dangerous and is opt-in for that reason. Teardown is
where the difference bites: an attached session must only DISCONNECT. Closing
the context or the browser would close the user's own windows, which is the one
thing this must never do.
"""

from __future__ import annotations

import queue
import re
import threading
import time
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
                 max_elements: int = 200, user_data_dir: str | None = None,
                 connect_url: str | None = None,
                 attach_factory: Callable | None = None,
                 bridge=None, extension_factory: Callable | None = None):
        """launch_factory(headless, executable_path, viewport, user_data_dir)
        -> (teardown, page) is called ON THE DRIVER THREAD to produce a
        Playwright page; the default uses real Playwright. Tests inject a fake
        to exercise all the routing, ref-tracking and snapshot logic without a
        real Chromium.

        user_data_dir, when set, launches a PERSISTENT context rooted at that
        directory: cookies and logins survive across sessions and app
        restarts. It's a dedicated agent profile (never the user's own
        browser); the user logs into chosen sites once and the agent reuses
        them. None (the default) keeps the fully throwaway profile.

        bridge, when set, is the extension backend: an ExtensionBridge the
        user's own browser has dialled into. Nothing is launched and nothing is
        attached to -- the browser is already running with the extension inside
        it -- so this is the mode that needs no relaunch, no flags and no
        second profile. It wins over connect_url when both are set, because it
        is the one that works without asking anything of the user.

        connect_url is the older way to reach the user's browser: attach over
        the DevTools protocol to one that was STARTED with the port open. It
        still works and is still tested, but it can only ever be used by
        quitting the browser and reopening it, which is why the extension
        exists.

        headless and user_data_dir have nothing to say in either of those
        cases -- the window, its profile and its visibility are the user's --
        and are ignored.

        attach_factory(connect_url, viewport) and extension_factory(bridge,
        viewport), both -> (teardown, page), are the injectable seams for the
        two. They are separate from launch_factory because neither is a kind of
        launching: they take different inputs and, crucially, their teardown
        must not close anything."""
        self.headless = headless
        self.viewport = viewport
        self.executable_path = executable_path
        self.status = status
        self._launch_factory = launch_factory or _real_launch
        self.max_elements = max_elements
        self.user_data_dir = user_data_dir
        self.connect_url = (connect_url or "").strip() or None
        self._attach_factory = attach_factory or _real_attach
        self.bridge = bridge
        self._extension_factory = extension_factory or _real_extension

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

    @property
    def is_attached(self) -> bool:
        """Driving a browser the user started, rather than one we launched.

        True for both ways of doing that -- through the extension, and over a
        DevTools port -- because everything that keys off it cares only WHOSE
        browser this is. The Browser Agent must be told it is acting as the
        user either way.
        """
        return self.bridge is not None or self.connect_url is not None

    def _run(self) -> None:
        try:
            if self.bridge is not None:
                self._teardown, self._page = self._extension_factory(
                    self.bridge, self.viewport)
                self._sync_viewport()
            elif self.connect_url:
                self._teardown, self._page = self._attach_factory(
                    self.connect_url, self.viewport)
                self._sync_viewport()
            else:
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

    def _sync_viewport(self) -> None:
        """Adopt the real window's size, for an attached browser only.

        Every snapshot header quotes self.viewport, and browser_click_at
        REJECTS coordinates outside it. A window we launched is whatever size
        we asked for, so those agree by construction -- but a window the user
        already had open is whatever size they left it, and a stale 1280x800
        would tell the model the wrong thing and then refuse the right click
        for being "outside the viewport". Best-effort: a page that will not
        answer keeps the default rather than failing the attach.
        """
        size = None
        try:
            size = self._page.viewport_size
        except Exception:
            size = None
        if not size:
            try:
                size = self._page.evaluate(
                    "() => ({width: window.innerWidth, height: window.innerHeight})")
            except Exception:
                return
        try:
            w, h = int(size["width"]), int(size["height"])
        except (TypeError, KeyError, ValueError):
            return
        if w > 0 and h > 0:
            self.viewport = (w, h)

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


# --------------------------------------------------------------------- #
# Driving the browser the user already has open, through the extension.
#
# Everything above talks to a Playwright Page. Rather than teach every _op_*
# a second way to do its job, the extension gets an object with the same
# surface -- the ops cannot tell the difference, and the snapshot, the ref
# tracking, the error messages and the Browser Agent's prompt all stay exactly
# as they are.
#
# The surface is small because the ops only ever use a dozen calls, and the
# element handle only needs six.


class _ExtHandle:
    """One element, addressed by its data-mnm-ref stamp.

    Playwright hands back a live handle; this re-resolves the ref on every
    call instead, which is the same guarantee _locate() was already after --
    never act through a stale handle -- and it means nothing has to be kept
    alive across the wire.
    """

    def __init__(self, page, ref: int):
        self._page = page
        self._ref = ref

    def is_enabled(self) -> bool:
        return bool(self._page._call("enabled", ref=self._ref))

    def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
        pass    # the click action scrolls; there is nothing separate to do

    def click(self, timeout: int = 0) -> None:
        self._page._call("click", ref=self._ref)

    def fill(self, text: str, timeout: int = 0) -> None:
        self._page._call("fill", ref=self._ref, text=text, submit=False)

    def press(self, key: str) -> None:
        self._page._call("press", key=key)

    def select_option(self, label=None, value=None) -> None:
        self._page._call("select", ref=self._ref,
                         text=label if label is not None else value)


class _ExtMouse:
    def __init__(self, page):
        self._page = page

    def click(self, x, y) -> None:
        self._page._call("click_at", x=x, y=y)


class _ExtKeyboard:
    def __init__(self, page):
        self._page = page

    def press(self, key: str) -> None:
        self._page._call("press", key=key)


class ExtensionPage:
    """A Playwright-Page-shaped view of the user's active tab."""

    def __init__(self, bridge):
        self._bridge = bridge
        self._url = ""
        self._title = ""
        self.mouse = _ExtMouse(self)
        self.keyboard = _ExtKeyboard(self)
        self.viewport_size = None

    def _call(self, command: str, **params):
        from .extension_bridge import BridgeError
        try:
            return self._bridge.call(command, **params)
        except BridgeError as e:
            raise BrowserError(str(e))

    # -- what the ops use --------------------------------------------- #

    def _refresh(self) -> dict:
        info = self._call("info") or {}
        self._url = info.get("url") or self._url
        self._title = info.get("title") or ""
        w, h = info.get("width"), info.get("height")
        if w and h:
            self.viewport_size = {"width": int(w), "height": int(h)}
        return info

    @property
    def url(self) -> str:
        return self._url

    def title(self) -> str:
        return self._title

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
        self._call("navigate", url=url, timeout=45)
        self._refresh()

    def go_back(self, wait_until: str = "", timeout: int = 0) -> None:
        self._call("back", timeout=45)
        self._refresh()

    def evaluate(self, js, arg=None):
        """Only ever called with SNAPSHOT_JS.

        MV3 forbids unsafe-eval, so the extension cannot run a script it was
        handed -- it holds a GENERATED copy of exactly this function instead
        (scripts/gen_extension_page.py). Anything else reaching here is a
        caller assuming a general evaluate() that does not exist, and saying so
        is better than silently snapshotting.
        """
        if "__mnmNextRef" not in str(js):
            raise BrowserError(
                "The extension backend can only run the page snapshot, not "
                "arbitrary JavaScript (Chrome forbids extensions from "
                "evaluating strings).")
        self._refresh()
        return self._call("snapshot")

    def inner_text(self, selector: str) -> str:
        self._refresh()
        return self._call("text", max=200_000) or ""

    def screenshot(self, path: str = ""):
        import base64
        data_url = self._call("screenshot", timeout=45) or ""
        _, _, b64 = data_url.partition(",")
        try:
            png = base64.b64decode(b64)
        except Exception:
            raise BrowserError("The browser returned an unreadable screenshot.")
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(png)
            return path
        return png

    def query_selector(self, selector: str):
        ref = _ref_from_selector(selector)
        if ref is None or not self._call("exists", ref=ref):
            return None
        return _ExtHandle(self, ref)

    def wait_for_timeout(self, ms) -> None:
        time.sleep(max(0.0, float(ms) / 1000.0))

    def is_closed(self) -> bool:
        return False


def _ref_from_selector(selector: str):
    """The ops address elements as [data-mnm-ref="N"]; pull N back out.

    Kept to that one shape on purpose. A general selector would have to be
    forwarded into the page and interpolated into a query, and there is no
    reason to open that up when the only caller stamps the refs itself.
    """
    m = re.search(r'data-mnm-ref="(\d+)"', selector or "")
    return int(m.group(1)) if m else None


def _real_extension(bridge, viewport):
    """Driver-thread 'launcher' for the extension backend.

    There is nothing to launch: the browser is already running and the
    extension is already in it. Teardown closes nothing at all -- it is the
    user's browser, and the socket outlives any one session because other
    chats use it too.
    """
    page = ExtensionPage(bridge)
    page._refresh()          # fails loudly here if nothing is connected

    def teardown():
        pass

    return teardown, page

# --------------------------------------------------------------------- #
# Attaching to a browser the user is already running.

DEBUG_PORT_HINT = (
    "A browser can only be attached to if it was STARTED with the DevTools "
    "port open -- there is no way to switch it on afterwards, so an already-"
    "running Chrome has to be quit and reopened. Current Chrome also refuses "
    "the port when it would use the default profile directory, so "
    "--user-data-dir is not optional; point it at a copy of your profile to "
    "keep your logins.\n"
    "  macOS:   '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' "
    "--remote-debugging-port=9222 --user-data-dir=\"$HOME/chrome-agent\"\n"
    "  Windows: & \"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\" "
    "--remote-debugging-port=9222 --user-data-dir=\"$env:USERPROFILE\\chrome-agent\"\n"
    "  Linux:   google-chrome --remote-debugging-port=9222 "
    "--user-data-dir=\"$HOME/chrome-agent\""
)


def _attached_page(browser):
    """The tab the user is actually looking at.

    Playwright has no notion of an active tab: `context.pages` is creation
    order, so driving pages[0] means driving whatever they opened first, which
    is rarely the window in front of them. The page itself knows, though --
    a foreground tab reports document.visibilityState 'visible' and every
    background tab reports 'hidden'. That is a real signal rather than a guess,
    so it is what decides.

    Falling back: the most recently opened page, then a new tab. A new tab is
    the last resort and is deliberately left behind at teardown -- closing tabs
    in someone's own browser is exactly the surprise this feature must avoid.
    """
    pages = []
    for ctx in browser.contexts:
        for pg in ctx.pages:
            try:
                if pg.is_closed():
                    continue
            except Exception:
                pass
            pages.append(pg)
    for pg in pages:
        try:
            if pg.evaluate("() => document.visibilityState") == "visible":
                return pg
        except Exception:
            continue
    if pages:
        return pages[-1]
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return ctx.new_page()


def _real_attach(connect_url: str, viewport):
    """Default driver-thread attacher: connect to a running browser over CDP
    and hand back the tab the user is looking at.

    Teardown stops the Playwright driver and NOTHING else. `context.close()`
    or `browser.close()` would reach across the connection and shut the user's
    own windows; dropping the driver closes the socket and leaves their browser
    exactly as it was.
    """
    from .browser import _install_packages, packages_installed
    if not packages_installed():
        _install_packages()
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(connect_url, timeout=10_000)
    except Exception as e:
        pw.stop()
        raise BrowserError(
            f"Couldn't attach to a browser at {connect_url}: "
            f"{str(e).splitlines()[0]}\n\n{DEBUG_PORT_HINT}")
    try:
        page = _attached_page(browser)
    except Exception as e:
        pw.stop()
        raise BrowserError(
            f"Attached to {connect_url} but found no page to drive: {e}")

    def teardown():
        pw.stop()

    return teardown, page
