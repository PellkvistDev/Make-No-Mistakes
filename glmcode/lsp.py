"""Language-server intelligence: real type errors, diagnostics and symbol
navigation from a project's language server (pyright, tsserver, gopls, …),
spoken over the Language Server Protocol.

Why: the agent otherwise learns a change is broken only when tests run. A
language server reports undefined names, type errors, unused imports and the
like *statically* -- often instantly and without executing anything -- which is
the biggest correctness lever left for a weak model. This module is the client
half: JSON-RPC framing over a server subprocess's stdio, the initialize
handshake, didOpen + publishDiagnostics collection, and definition/hover
requests. It degrades to a no-op when no server is installed.

Everything here is testable without a real server: the framing is pure, and the
client runs end-to-end against a scripted fake server subprocess in the tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

from .tools import NO_WINDOW_KWARGS

# Language -> candidate server argv, first installed one wins. Kept small and
# explicit; adding a language is one line.
LANGUAGE_SERVERS: dict[str, list[list[str]]] = {
    "python": [["pyright-langserver", "--stdio"], ["pylsp"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "javascript": [["typescript-language-server", "--stdio"]],
    "rust": [["rust-analyzer"]],
    "go": [["gopls"]],
}

EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".rs": "rust", ".go": "go",
}

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def language_for(path: str) -> str | None:
    return EXT_LANG.get(Path(path).suffix.lower())


def server_command(language: str) -> list[str] | None:
    """The first installed server for `language`, or None."""
    for argv in LANGUAGE_SERVERS.get(language, []):
        if shutil.which(argv[0]):
            return argv
    return None


def available_for(path: str) -> bool:
    lang = language_for(path)
    return bool(lang and server_command(lang))


# --------------------------------------------------------------------- #
# JSON-RPC framing (pure -- unit-tested directly)

def encode_message(obj: dict) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_message(stream) -> dict | None:
    """Read one framed LSP message from a binary stream, or None at EOF."""
    headers = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break                       # blank line ends the headers
        if b":" in line:
            k, _, v = line.partition(b":")
            headers[k.strip().lower()] = v.strip()
    try:
        length = int(headers.get(b"content-length", b"0"))
    except ValueError:
        return None
    body = stream.read(length)
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return None


def to_uri(path: Path) -> str:
    return Path(path).resolve().as_uri()


# --------------------------------------------------------------------- #
# Client

class LspClient:
    """One language-server subprocess and the JSON-RPC conversation with it."""

    def __init__(self, argv: list[str], root: Path):
        self.argv = argv
        self.root = Path(root).resolve()
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._pending: dict[int, dict] = {}   # id -> {"event", "result", "error"}
        self._lock = threading.Lock()
        self._diagnostics: dict[str, list] = {}
        self._diag_events: dict[str, threading.Event] = {}
        self._opened: set[str] = set()
        self._alive = False

    # -- lifecycle --------------------------------------------------------- #

    def start(self, timeout: float = 20.0) -> bool:
        try:
            self._proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(self.root), **NO_WINDOW_KWARGS,
            )
        except OSError:
            return False
        self._alive = True
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.request("initialize", {
                "processId": None,
                "rootUri": to_uri(self.root),
                "capabilities": {
                    "textDocument": {
                        "publishDiagnostics": {},
                        "hover": {"contentFormat": ["plaintext"]},
                        "definition": {},
                    }
                },
                "workspaceFolders": [{"uri": to_uri(self.root), "name": self.root.name}],
            }, timeout=timeout)
        except LspError:
            self.stop()
            return False
        self.notify("initialized", {})
        return True

    def stop(self) -> None:
        self._alive = False
        if self._proc is None:
            return
        try:
            self.notify("exit", None)
        except Exception:
            pass
        try:
            self._proc.terminate()
        except Exception:
            pass
        self._proc = None

    # -- transport --------------------------------------------------------- #

    def _write(self, obj: dict) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise LspError("server is not running")
        self._proc.stdin.write(encode_message(obj))
        self._proc.stdin.flush()

    def request(self, method: str, params, timeout: float = 10.0):
        with self._lock:
            self._id += 1
            rid = self._id
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            self._pending.pop(rid, None)
            raise LspError(f"{method} timed out")
        if slot["error"] is not None:
            raise LspError(str(slot["error"]))
        return slot["result"]

    def notify(self, method: str, params) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _reader(self) -> None:
        stream = self._proc.stdout if self._proc else None
        while self._alive and stream is not None:
            msg = read_message(stream)
            if msg is None:
                break
            if "id" in msg and ("result" in msg or "error" in msg):
                slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot["result"] = msg.get("result")
                    slot["error"] = msg.get("error")
                    slot["event"].set()
            elif msg.get("method") == "textDocument/publishDiagnostics":
                p = msg.get("params", {})
                uri = p.get("uri", "")
                self._diagnostics[uri] = p.get("diagnostics", [])
                self._diag_events.setdefault(uri, threading.Event()).set()
            # other server->client requests/notifications are ignored
        self._alive = False

    # -- features ---------------------------------------------------------- #

    def _open(self, path: Path) -> str:
        uri = to_uri(path)
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        lang = language_for(str(path)) or "plaintext"
        if uri in self._opened:
            # bump the version so the server re-analyses the current contents
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": 2},
                "contentChanges": [{"text": text}],
            })
        else:
            self._diag_events[uri] = threading.Event()
            self.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": lang, "version": 1, "text": text},
            })
            self._opened.add(uri)
        return uri

    def diagnostics(self, path: Path, timeout: float = 8.0) -> list[dict]:
        """Open `path` and return the server's diagnostics for it: a list of
        {severity, line, character, message, source}. Waits (bounded) for the
        server's async publishDiagnostics."""
        uri = self._open(path)
        ev = self._diag_events.get(uri)
        if ev is not None:
            ev.wait(timeout)
        out = []
        for d in self._diagnostics.get(uri, []):
            rng = (d.get("range") or {}).get("start", {})
            out.append({
                "severity": _SEVERITY.get(d.get("severity", 1), "error"),
                "line": rng.get("line", 0) + 1,
                "character": rng.get("character", 0) + 1,
                "message": d.get("message", ""),
                "source": d.get("source", ""),
            })
        return out

    def definition(self, path: Path, line: int, character: int) -> list[dict]:
        uri = self._open(path)
        res = self.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, character - 1)},
        })
        return _locations(res)

    def hover(self, path: Path, line: int, character: int) -> str:
        uri = self._open(path)
        res = self.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, line - 1), "character": max(0, character - 1)},
        })
        return _hover_text(res)


class LspError(Exception):
    pass


def _locations(res) -> list[dict]:
    if not res:
        return []
    items = res if isinstance(res, list) else [res]
    out = []
    for it in items:
        uri = it.get("uri") or it.get("targetUri") or ""
        rng = it.get("range") or it.get("targetRange") or {}
        start = rng.get("start", {})
        try:
            path = Path(uri.replace("file://", "")).as_posix() if uri.startswith("file://") else uri
        except Exception:
            path = uri
        out.append({"path": path, "line": start.get("line", 0) + 1,
                    "character": start.get("character", 0) + 1})
    return out


def _hover_text(res) -> str:
    if not res:
        return ""
    contents = res.get("contents")
    if isinstance(contents, dict):
        return str(contents.get("value", "")).strip()
    if isinstance(contents, list):
        parts = [c.get("value", "") if isinstance(c, dict) else str(c) for c in contents]
        return "\n".join(p for p in parts if p).strip()
    return str(contents or "").strip()


# --------------------------------------------------------------------- #
# Per-workdir manager: lazily spawn one server per language, reuse it.

class LspManager:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._clients: dict[str, LspClient | None] = {}
        self._lock = threading.Lock()

    def _client_for(self, path: str) -> LspClient | None:
        lang = language_for(path)
        if not lang:
            return None
        with self._lock:
            if lang in self._clients:
                return self._clients[lang]
            argv = server_command(lang)
            client = None
            if argv:
                c = LspClient(argv, self.root)
                client = c if c.start() else None
            self._clients[lang] = client
            return client

    def diagnostics(self, path: str) -> tuple[bool, list[dict]]:
        """(available, diagnostics). available is False when no server is
        installed for this file type, so callers can say so rather than
        pretending the file is clean."""
        c = self._client_for(path)
        if c is None:
            return False, []
        try:
            return True, c.diagnostics(Path(path))
        except LspError:
            return True, []

    def definition(self, path: str, line: int, character: int) -> tuple[bool, list[dict]]:
        c = self._client_for(path)
        if c is None:
            return False, []
        try:
            return True, c.definition(Path(path), line, character)
        except LspError:
            return True, []

    def hover(self, path: str, line: int, character: int) -> tuple[bool, str]:
        c = self._client_for(path)
        if c is None:
            return False, ""
        try:
            return True, c.hover(Path(path), line, character)
        except LspError:
            return True, ""

    def shutdown(self) -> None:
        with self._lock:
            for c in self._clients.values():
                if c is not None:
                    c.stop()
            self._clients.clear()
