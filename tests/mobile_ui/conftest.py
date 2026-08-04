"""Harness for driving the real phone app in a headless browser.

Why this exists: mobile/app.js is the app's largest file and the only one that
touches the DOM, and it had no automated coverage at all. agent-core.test.js
cannot reach it -- that suite tests pure logic with no browser. The gap was not
theoretical: a chat resume silently wiped the queue of work parked for the
desktop, and the next save pushed the empty queue back to the sync store,
erasing it on every device. Nothing caught it because nothing drove the app.

The stub is a small in-memory GitHub Contents API rather than a mock of the
app's own functions, so everything under the fetch boundary is the real thing:
real AES-GCM, real sync store, real compare-and-swap on sha, real app wiring.
A test that stubbed openSync() would have passed straight through the bug above.
"""

import functools
import http.server
import socket
import threading
from pathlib import Path

import pytest

MOBILE = Path(__file__).resolve().parents[2] / "mobile"

playwright = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from playwright.sync_api import sync_playwright  # noqa: E402


# A fake GitHub that keeps files in memory and honours the sha precondition,
# because sha-gated PUT is the only compare-and-swap the real app has and half
# the sync behaviour is built on it.
FAKE_GITHUB = r"""() => {
  const files = {};            // path -> {content(b64), sha}
  let n = 0;
  const sha = () => "sha" + (++n);
  const enc = (s) => btoa(String.fromCharCode(...new TextEncoder().encode(s)));
  window.__files = files;
  window.__sent = [];          // model requests, newest last
  window.__modelQueue = [];    // scripted replies; last one repeats
  window.__seedFile = (p, text) => { files[p] = { content: enc(text), sha: sha() }; };

  // Holding a reply open has to live in here rather than in a wrapper the test
  // installs later: makeModel() captures global.fetch when it is constructed,
  // so anything that replaces window.fetch after the app boots is never seen.
  window.__holdNext = false;
  window.__inFlight = false;
  window.__release = null;

  const j = (o, status = 200) => ({
    ok: status < 400, status,
    json: async () => o,
    text: async () => JSON.stringify(o),
  });

  window.fetch = async (url, init = {}) => {
    const u = new URL(String(url), "https://api.github.com");
    const p = u.pathname;
    const method = (init.method || "GET").toUpperCase();
    const body = init.body ? JSON.parse(init.body) : null;

    if (p.includes("/chat/completions")) {
      window.__sent.push(body);
      if (window.__holdNext) {
        window.__holdNext = false;
        window.__inFlight = true;
        await new Promise((res) => { window.__release = res; });
        window.__inFlight = false;
      }
      const q = window.__modelQueue;
      const reply = q.length > 1 ? q.shift() : (q[0] || { role: "assistant", content: "ok" });
      return j({ choices: [{ message: reply }] });
    }
    if (p === "/user") return j({ login: "you" });
    if (p === "/user/repos" && method === "GET")
      return j([{ full_name: "you/app", name: "app", owner: { login: "you" }, default_branch: "main" }]);
    if (p === "/user/repos" && method === "POST") return j({ name: body.name });
    if (/^\/repos\/[^/]+\/[^/]+$/.test(p)) return j({ name: p.split("/").pop() });

    const m = p.match(/^\/repos\/[^/]+\/[^/]+\/contents\/(.*)$/);
    if (m) {
      const path = decodeURIComponent(m[1]);
      if (method === "GET") {
        const f = files[path];
        if (!f) return j({ message: "Not Found" }, 404);
        return j({ content: f.content, sha: f.sha, size: atob(f.content).length });
      }
      if (method === "PUT") {
        const cur = files[path];
        // The precondition the whole store depends on: a blind write over
        // someone else's newer version must be rejected, not silently win.
        if (cur && body.sha !== cur.sha) return j({ message: "does not match" }, 409);
        if (!cur && body.sha) return j({ message: "does not match" }, 409);
        files[path] = { content: body.content, sha: sha() };
        return j({ content: { sha: files[path].sha } });
      }
      if (method === "DELETE") {
        const cur = files[path];
        if (cur && body.sha !== cur.sha) return j({ message: "does not match" }, 409);
        delete files[path];
        return j({});
      }
    }
    if (p.includes("/git/trees/")) return j({ tree: [{ type: "blob", path: "a.py", size: 8 }] });
    if (p.includes("/git/ref/heads/")) return j({ object: { sha: "commitsha" } });
    if (p.includes("/git/")) return j({ sha: sha() });
    return j({});
  };
}"""


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture(scope="session")
def app_url():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    handler = functools.partial(Quiet, directory=str(MOBILE))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = _Server(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/index.html"
    srv.shutdown()


def _launch(pw):
    """Prefer the browser Playwright installed for itself; fall back to one
    already on the box.

    CI runs `playwright install chromium` and takes the first branch. Some dev
    containers ship a Chromium under a different build number than the pinned
    Playwright wants, and refusing to run there would mean these tests only
    ever get exercised in CI -- which is how app.js ended up untested to begin
    with.
    """
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
    """Function-scoped on purpose, despite the extra launch per test.

    Playwright's sync API cannot be re-entered on a thread that already has a
    sync_playwright() context open. Holding one for the whole session made
    glmcode.browser.ready() -- which probes by launching -- throw, get caught,
    and report "chromium missing", so tests elsewhere in the suite tried to
    download a browser mid-run. Session scope saved a few seconds here and
    broke six tests over there.
    """
    with sync_playwright() as pw:
        b = _launch(pw)
        yield b
        b.close()


class Phone:
    """The app, driven the way a person drives it."""

    def __init__(self, page):
        self.page = page
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))

    def open_at(self, url):
        """Re-open the app at a URL that differs only by fragment.

        goto() alone would be a same-document navigation -- the hash changes,
        no script re-runs, and the app never sees the pairing token. The reload
        is what makes it a real launch. The fetch stub is an init script, so it
        is redefined automatically; it still has to be invoked.
        """
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.reload(wait_until="domcontentloaded")
        self.page.evaluate("window.__fakeGitHub()")
        return self

    def setup(self, sync_pass="sync passphrase"):
        """Through first-run setup and into a connected repo, with sync on."""
        p = self.page
        p.fill("#in-model-key", "modelkey")
        p.fill("#in-gh-token", "ghtoken")
        p.fill("#in-pin", "1234")
        p.fill("#in-pin2", "1234")
        p.click("#btn-save-setup")
        p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
        if sync_pass:
            self.enable_sync(sync_pass, "btn-repo-settings")
        p.wait_for_selector(".repo-list li", timeout=15000)
        p.click(".repo-list li")
        p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
        return self

    def enable_sync(self, passphrase, settings_btn="btn-chat-settings"):
        """Through the real settings sheet, not a back door -- the verify-then-
        store order in that handler is itself behaviour worth exercising."""
        p = self.page
        p.click("#" + settings_btn)
        p.wait_for_selector("#settings-backdrop:not([hidden])", timeout=15000)
        # Flipping the toggle with no passphrase saved is what opens the sheet;
        # the "change passphrase" button is hidden until sync is already on.
        p.click("#set-sync")
        p.wait_for_selector("#syncpass-backdrop:not([hidden])", timeout=15000)
        p.fill("#in-syncpass", passphrase)
        p.fill("#in-syncpass2", passphrase)
        p.click("#btn-syncpass-save")
        p.wait_for_selector("#syncpass-backdrop", state="hidden", timeout=15000)
        self.close_settings()
        return self

    def close_settings(self):
        p = self.page
        if p.is_visible("#settings-backdrop"):
            p.click("#btn-settings-done")
            p.wait_for_selector("#settings-backdrop", state="hidden", timeout=15000)
        return self

    def reply(self, *messages):
        """Script the model's replies; the last one repeats."""
        self.page.evaluate("(m) => { window.__modelQueue = m; }", list(messages))
        return self

    def send(self, text):
        p = self.page
        p.fill("#in-prompt", text)
        p.click("#btn-send")
        return self

    def wait_idle(self, timeout=15000):
        self.page.wait_for_function(
            "() => document.getElementById('btn-stop').hidden", timeout=timeout)
        return self

    def sent_messages(self):
        """The messages array of the most recent model request."""
        return self.page.evaluate(
            "() => { const s = window.__sent; return s.length ? s[s.length - 1].messages : null; }")

    def stored_chats(self, passphrase="sync passphrase"):
        """Read the sync store back through a second, independent reader.

        Deliberately not a peek at the app's own session object: this opens the
        store from scratch with only the passphrase, which is exactly what the
        other device does. If a chat survives this it survives the trip.
        """
        return self.page.evaluate("""async (pass) => {
          const AC = window.AgentCore;
          const probe = AC.makeGitHub({ token: "ghtoken", owner: "", repo: "" });
          const { owner, repo } = await AC.ensureSyncRepo(probe);
          const gh = AC.makeGitHub({ token: "ghtoken", owner, repo, branch: AC.SYNC_REPO_BRANCH });
          const { store } = await AC.openSync(gh, pass);
          const out = [];
          for (const c of await store.list()) out.push(await store.load(c.id));
          return out;
        }""", passphrase)


@pytest.fixture
def phone(browser, app_url):
    ctx = browser.new_context(viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    page.add_init_script("window.__fakeGitHub = " + FAKE_GITHUB)
    page.goto(app_url, wait_until="domcontentloaded")
    page.evaluate("window.__fakeGitHub()")
    page.wait_for_selector("#screen-setup:not([hidden])", timeout=15000)
    yield Phone(page)
    ctx.close()
