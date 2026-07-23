"""LSP client: pure framing, server-command detection, and a full end-to-end
conversation (initialize -> didOpen -> publishDiagnostics, definition, hover)
against a scripted fake language server subprocess -- no real server needed."""

import io
import sys
import textwrap

import pytest

from glmcode import lsp

# A minimal fake language server that speaks LSP framing on stdio. It replies to
# initialize, emits one diagnostic on didOpen, and answers definition/hover.
FAKE_SERVER = textwrap.dedent('''
    import json, sys
    inp, out = sys.stdin.buffer, sys.stdout.buffer

    def read():
        headers = {}
        while True:
            line = inp.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
        n = int(headers.get(b"content-length", b"0"))
        return json.loads(inp.read(n).decode("utf-8"))

    def send(obj):
        body = json.dumps(obj).encode("utf-8")
        out.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        out.flush()

    while True:
        msg = read()
        if msg is None:
            break
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {}}})
        elif method == "textDocument/didOpen":
            uri = msg["params"]["textDocument"]["uri"]
            send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
                  "params": {"uri": uri, "diagnostics": [
                      {"range": {"start": {"line": 2, "character": 4},
                                 "end": {"line": 2, "character": 9}},
                       "severity": 1, "source": "fake", "message": "undefined name 'oops'"}]}})
        elif method == "textDocument/definition":
            uri = msg["params"]["textDocument"]["uri"]
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "uri": uri, "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 3}}}})
        elif method == "textDocument/hover":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"contents": {"kind": "plaintext", "value": "int"}}})
        elif method == "exit":
            break
''')


# --------------------------------------------------------------- framing --

def test_encode_read_roundtrip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"a": 1}}
    stream = io.BytesIO(lsp.encode_message(payload))
    assert lsp.read_message(stream) == payload


def test_read_message_eof():
    assert lsp.read_message(io.BytesIO(b"")) is None


def test_language_and_server_detection(monkeypatch):
    assert lsp.language_for("a/b/foo.py") == "python"
    assert lsp.language_for("x.tsx") == "typescript"
    assert lsp.language_for("notes.txt") is None
    monkeypatch.setattr(lsp.shutil, "which", lambda name: "/usr/bin/" + name
                        if name == "pyright-langserver" else None)
    assert lsp.server_command("python") == ["pyright-langserver", "--stdio"]
    monkeypatch.setattr(lsp.shutil, "which", lambda name: None)
    assert lsp.server_command("python") is None


# -------------------------------------------------------- end-to-end --

@pytest.fixture
def fake_server(tmp_path):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return [sys.executable, str(script)]


def test_client_diagnostics_definition_hover(fake_server, tmp_path):
    src = tmp_path / "mod.py"
    src.write_text("x = 1\ny = 2\n    oops\n", encoding="utf-8")
    client = lsp.LspClient(fake_server, tmp_path)
    assert client.start(timeout=15) is True
    try:
        diags = client.diagnostics(src, timeout=8)
        assert len(diags) == 1
        d = diags[0]
        assert d["severity"] == "error" and d["line"] == 3 and "oops" in d["message"]

        locs = client.definition(src, 1, 1)
        assert locs and locs[0]["line"] == 1

        assert client.hover(src, 1, 1) == "int"
    finally:
        client.stop()


def test_manager_reports_unavailable_without_server(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "server_command", lambda lang: None)
    mgr = lsp.LspManager(tmp_path)
    p = tmp_path / "a.py"
    p.write_text("x=1\n", encoding="utf-8")
    available, diags = mgr.diagnostics(str(p))
    assert available is False and diags == []


def test_manager_end_to_end(fake_server, tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, "server_command", lambda lang: fake_server)
    mgr = lsp.LspManager(tmp_path)
    p = tmp_path / "mod.py"
    p.write_text("x = 1\ny = 2\n    oops\n", encoding="utf-8")
    available, diags = mgr.diagnostics(str(p))
    assert available is True and len(diags) == 1 and diags[0]["severity"] == "error"
    mgr.shutdown()


def test_code_diagnostics_tool(fake_server, tmp_path, monkeypatch):
    import glmcode.tools as tools
    monkeypatch.setattr(lsp, "server_command", lambda lang: fake_server)
    tools._lsp_managers.clear()
    tools.set_workdir(tmp_path)
    p = tmp_path / "mod.py"
    p.write_text("x = 1\ny = 2\n    oops\n", encoding="utf-8")
    out = tools.code_diagnostics("mod.py")
    assert "error" in out and "oops" in out and "mod.py:3" in out


def test_code_diagnostics_tool_no_server(tmp_path, monkeypatch):
    import glmcode.tools as tools
    monkeypatch.setattr(lsp, "server_command", lambda lang: None)
    tools.set_workdir(tmp_path)
    p = tmp_path / "mod.py"
    p.write_text("x = 1\n", encoding="utf-8")
    out = tools.code_diagnostics("mod.py")
    assert "No language server" in out
