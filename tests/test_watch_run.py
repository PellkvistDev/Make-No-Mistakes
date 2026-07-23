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
