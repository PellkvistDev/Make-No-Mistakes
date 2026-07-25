"""'Watch it run': capture_page loads a running page and reports runtime console
errors, uncaught exceptions and failed requests. The end-to-end test serves a
page that deliberately errors and checks they're captured (real headless
Chromium); the tool-formatting test doesn't need a browser."""

import http.server
import os
import threading
from pathlib import Path

import pytest

from glmcode import browser

_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
needs_chromium = pytest.mark.skipif(
    not Path(_CHROMIUM).exists() or not browser.packages_installed(),
    reason="headless Chromium not available in this environment")

_HTML = (b"<!doctype html><html><body><h1>hi</h1><script>"
         b"console.error('boom-from-console');"
         b"throw new Error('kaboom-uncaught');"
         b"</script></body></html>")


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(_HTML)

    def log_message(self, *a):
        pass


@pytest.fixture
def served():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@needs_chromium
def test_capture_page_reports_runtime_errors(served, tmp_path, monkeypatch):
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    r = browser.capture_page(served, tmp_path / "shot.png", wait_seconds=1.0)
    assert not r["load_error"]
    assert any("kaboom-uncaught" in e for e in r["page_errors"])
    assert any("boom-from-console" in c for c in r["console"])
    assert r["screenshot"] and Path(r["screenshot"]).exists()


_API_FAIL_HTML = (
    b"<!doctype html><html><body><script>"
    b"console.error('deep object:', {status:500, detail:{reason:'db down'}});"
    b"console.log('log-level failure:', 404);"
    b"fetch('/api/bad').catch(()=>{});"
    b"</script></body></html>")


class _ApiFailHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/bad":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"db down"}')
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_API_FAIL_HTML)

    def log_message(self, *a):
        pass


@pytest.fixture
def api_fail_served():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _ApiFailHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@needs_chromium
def test_capture_page_surfaces_log_level_failures(api_fail_served, tmp_path, monkeypatch):
    """console.log(...) carrying error content used to be silently dropped --
    only 'error'/'warning' console TYPES were captured, but plenty of real code
    (and plenty of libraries) logs a failure at the 'log' level."""
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    r = browser.capture_page(api_fail_served, tmp_path / "s.png", wait_seconds=1.0)
    assert any("log-level failure" in c and "404" in c for c in r["console"]), r["console"]


@needs_chromium
def test_capture_page_serializes_nested_console_objects(api_fail_served, tmp_path, monkeypatch):
    """A console.error(msg, {nested: {object}}) used to collapse to '{a: Object}'
    -- exactly the detail (the actual error payload) needed to diagnose it."""
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    r = browser.capture_page(api_fail_served, tmp_path / "s.png", wait_seconds=1.0)
    assert any("db down" in c and "500" in c for c in r["console"]), r["console"]
    assert not any("Object]" in c or ": Object" in c for c in r["console"])


@needs_chromium
def test_capture_page_reports_failed_api_calls_with_detail(api_fail_served, tmp_path, monkeypatch):
    """A fetch() to an endpoint returning 500 is the single most common thing a
    developer sees break on localhost. It must show up in failed_requests with
    the URL, the status, and the response body -- not just silently absent
    (the old failed_requests only fired on network-level failures like DNS)."""
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    r = browser.capture_page(api_fail_served, tmp_path / "s.png", wait_seconds=1.0)
    hit = next((f for f in r["failed_requests"] if "/api/bad" in f), None)
    assert hit, r["failed_requests"]
    assert "500" in hit
    assert "db down" in hit


@needs_chromium
def test_capture_page_important_console_survives_a_noisy_page(tmp_path, monkeypatch):
    """error/warning must never be crowded out of the cap by routine log noise
    from a chatty app (React dev mode, verbose loggers, etc.)."""
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    noisy = ("".join(f"console.log('routine log {i}');" for i in range(60))
            + "console.error('the real problem');")
    html = f"<!doctype html><html><body><script>{noisy}</script></body></html>".encode()

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(html)
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = browser.capture_page(f"http://127.0.0.1:{srv.server_address[1]}/",
                                 tmp_path / "s.png", wait_seconds=1.0)
        assert any("the real problem" in c for c in r["console"]), r["console"]
    finally:
        srv.shutdown()


@needs_chromium
def test_capture_page_clean_page_has_no_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MNM_CHROMIUM_PATH", _CHROMIUM)
    clean = http.server.HTTPServer(("127.0.0.1", 0), type(
        "H", (http.server.BaseHTTPRequestHandler,), {
            "do_GET": lambda s: (s.send_response(200), s.end_headers(),
                                 s.wfile.write(b"<h1>ok</h1>")),
            "log_message": lambda *a: None,
        }))
    threading.Thread(target=clean.serve_forever, daemon=True).start()
    try:
        r = browser.capture_page(f"http://127.0.0.1:{clean.server_address[1]}/",
                                 tmp_path / "s.png", wait_seconds=0.5)
        assert not r["page_errors"] and not r["console"] and not r["load_error"]
    finally:
        clean.shutdown()


# -------------------------------------------------- tool output formatting --

def test_check_page_tool_reports_and_recovers(scripted_agent, tmp_path, monkeypatch):
    import glmcode.browser as br
    agent = scripted_agent()
    agent.workdir = tmp_path

    def fake_capture(url, out_path, wait_seconds=2.5, status=None):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"png")
        return {"screenshot": str(out_path), "console": ["[error] TypeError: x is undefined"],
                "page_errors": ["Error: kaboom"], "failed_requests": [], "load_error": ""}
    monkeypatch.setattr(br, "capture_page", fake_capture)
    out = agent._check_page_tool("http://localhost:3000")
    assert "kaboom" in out and "TypeError" in out and "fix them" in out


def test_check_page_tool_clean(scripted_agent, tmp_path, monkeypatch):
    import glmcode.browser as br
    agent = scripted_agent()
    agent.workdir = tmp_path
    monkeypatch.setattr(br, "capture_page", lambda *a, **k: {
        "screenshot": "", "console": [], "page_errors": [],
        "failed_requests": [], "load_error": ""})
    out = agent._check_page_tool("http://localhost:3000")
    assert "no console errors" in out


# --------------------------- unit tests for the formatting helpers (no browser) --

class _FakeArg:
    def __init__(self, value, raises=False):
        self._value, self._raises = value, raises

    def json_value(self):
        if self._raises:
            raise Exception("not JSON-serializable")
        return self._value

    def __str__(self):
        return f"<JSHandle {self._value!r}>"


class _FakeMsg:
    def __init__(self, args, text="fallback text"):
        self.args = args
        self.text = text


def test_console_message_text_uses_real_arg_values_not_shallow_text():
    from glmcode.browser import _console_message_text
    msg = _FakeMsg([_FakeArg("failed:"), _FakeArg({"status": 500, "detail": {"deep": 1}})])
    out = _console_message_text(msg)
    assert "500" in out and "deep" in out
    assert "Object" not in out


def test_console_message_text_falls_back_on_unserializable_args():
    from glmcode.browser import _console_message_text
    msg = _FakeMsg([_FakeArg(None, raises=True)], text="fallback text")
    out = _console_message_text(msg)
    assert "JSHandle" in out or out == "fallback text"


def test_console_message_text_falls_back_when_no_args():
    from glmcode.browser import _console_message_text
    msg = _FakeMsg([], text="the raw text")
    assert _console_message_text(msg) == "the raw text"


class _FakeRequest:
    def __init__(self, method):
        self.method = method


class _FakeResponse:
    def __init__(self, status, status_text, url, body=None, body_raises=False):
        self.status, self.status_text, self.url = status, status_text, url
        self._body, self._raises = body, body_raises
        self.request = _FakeRequest("GET")

    def text(self):
        if self._raises:
            raise Exception("no body available")
        return self._body or ""


def test_format_bad_response_includes_method_url_status_and_body():
    from glmcode.browser import _format_bad_response
    resp = _FakeResponse(500, "Internal Server Error",
                         "http://localhost:3000/api/data", body='{"error":"db down"}')
    out = _format_bad_response(resp)
    assert "GET" in out and "500" in out and "/api/data" in out and "db down" in out


def test_format_bad_response_survives_an_unreadable_body():
    from glmcode.browser import _format_bad_response
    resp = _FakeResponse(404, "Not Found", "http://localhost:3000/missing.png",
                         body_raises=True)
    out = _format_bad_response(resp)
    assert "404" in out and "missing.png" in out  # no crash, no body appended
