"""Harness for driving the desktop GUI in a headless browser.

glmcode/gui/web/app.js is 5591 lines -- the largest file in the project -- and
like mobile/app.js before it, nothing exercised it. The Python behind it is well
covered; the layer the user actually touches was not tested at all.

The seam is clean: the UI reaches Python only through window.pywebview.api, and
the app boots on a `pywebviewready` event. So a stub of that one object puts the
whole interface under test with the real markup, the real CSS and the real
5591 lines running.

The stub is a Proxy rather than 90 hand-written methods. Enumerating them would
rot the moment the API grows, and a missing one would surface as an obscure
undefined rather than a clear failure -- the same "list someone forgets to
extend" that cost a chat its transcript in the sync store.
"""

import functools
import http.server
import json
import socket
import threading
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "glmcode" / "gui" / "web"

pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

# Every api() call resolves; tests override the ones they care about. Calls are
# recorded so a test can assert what the UI asked the backend to do, which is
# most of what this layer's job actually is.
BRIDGE = r"""() => {
  window.__calls = [];
  window.__replies = {};          // name -> value (or [values] consumed in order)
  window.__reply = (name, value) => { window.__replies[name] = value; };

  const respond = (name, args) => {
    window.__calls.push({ name, args });
    let r = window.__replies[name];
    if (Array.isArray(r) && r.__queue) r = r.length > 1 ? r.shift() : r[0];
    return Promise.resolve(r === undefined ? {} : r);
  };
  window.pywebview = {
    api: new Proxy({}, {
      get: (_t, name) => {
        if (name === "then") return undefined;   // not a thenable
        return (...args) => respond(String(name), args);
      },
    }),
  };
}"""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture(scope="session")
def app_url():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(WEB))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = _Server(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index.html"
    srv.shutdown()


def _launch(pw):
    """Same fallback as the mobile harness: prefer Playwright's own browser,
    accept one already on the box, so these run outside CI too."""
    args = ["--no-sandbox"]
    try:
        return pw.chromium.launch(args=args)
    except Exception:
        found = sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
        if not found:
            raise
        return pw.chromium.launch(executable_path=str(found[-1]), args=args)


@pytest.fixture
def browser():
    # Function-scoped: a session-wide sync_playwright context makes
    # glmcode.browser.ready() -- which probes by launching -- throw and report
    # "chromium missing", sending other tests off to download one mid-run.
    with sync_playwright() as pw:
        b = _launch(pw)
        yield b
        b.close()


class Desktop:
    """The GUI, driven the way a person drives it."""

    def __init__(self, page):
        self.page = page
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def reply(self, name, value):
        """Script what one backend method returns."""
        self.page.evaluate("([n, v]) => window.__reply(n, v)", [name, value])
        return self

    def calls(self, name=None):
        """What the UI asked the backend to do, in order."""
        got = self.page.evaluate("() => window.__calls")
        return [c for c in got if name is None or c["name"] == name]

    def boot(self, **replies):
        for name, value in replies.items():
            self.reply(name, value)
        self.page.evaluate("() => window.dispatchEvent(new Event('pywebviewready'))")
        self.page.wait_for_timeout(400)
        return self


@pytest.fixture
def desktop(browser, app_url):
    ctx = browser.new_context(viewport={"width": 1280, "height": 860})
    page = ctx.new_page()
    page.add_init_script("window.__bridge = " + BRIDGE)
    page.goto(app_url, wait_until="domcontentloaded")
    page.evaluate("window.__bridge()")
    yield Desktop(page)
    ctx.close()
