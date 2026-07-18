"""MCP (Model Context Protocol) client: connect external tool servers.

Speaks the MCP stdio transport -- newline-delimited JSON-RPC 2.0 over a child
process's stdin/stdout (the dominant way MCP servers ship: `npx ...`,
`uvx ...`, a python script). Each configured server is spawned once, hand-
shaken (initialize -> notifications/initialized), asked for its tools, and
those tools are exposed to the agent as ordinary function-call tools with
namespaced names. Calls route back through tools/call.

Everything is best-effort and isolated: a dead or misbehaving server never
breaks a chat -- its tools just disappear and calls raise a ToolError the
model can react to.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .tools import NO_WINDOW_KWARGS
from .errors import ToolError as ToolErrorBase, ErrorSeverity

PROTOCOL_VERSION = "2025-03-26"
INIT_TIMEOUT = 45.0    # npx/uvx may download the server on first run
LIST_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0

_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize(name: str) -> str:
    return _NAME_SANITIZE_RE.sub("_", name)[:40]


@dataclass
class McpTool:
    server: str          # server name (config)
    name: str            # the tool's real name on the server
    exposed: str         # namespaced name shown to the model
    description: str
    input_schema: dict


class McpServer:
    """One running MCP server (stdio transport)."""

    def __init__(self, name: str, command: str):
        self.name = name
        self.command = command
        self.proc: subprocess.Popen | None = None
        self.tools: list[McpTool] = []
        self.error: str = ""
        self._lock = threading.Lock()
        self._next_id = 1
        self._pending: dict[int, dict] = {}  # id -> {"event", "msg"}

    # -- lifecycle ----------------------------------------------------- #

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        """Spawn + handshake + list tools. Sets self.error on any failure."""
        self.error = ""
        try:
            # shell=True so `npx`/`uvx` resolve on Windows (npx.cmd) and the
            # user can paste a plain command line.
            self.proc = subprocess.Popen(
                self.command, shell=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                **NO_WINDOW_KWARGS,
            )
        except OSError as e:
            self.error = f"failed to start: {e}"
            return
        threading.Thread(target=self._read_loop, daemon=True,
                         name=f"mcp-{self.name}").start()
        try:
            self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Make No Mistakes", "version": "1.0"},
            }, timeout=INIT_TIMEOUT)
            self._notify("notifications/initialized")
            result = self._rpc("tools/list", {}, timeout=LIST_TIMEOUT)
            self.tools = []
            for t in result.get("tools", []):
                exposed = f"mcp_{_sanitize(self.name)}_{_sanitize(t.get('name', ''))}"[:64]
                self.tools.append(McpTool(
                    server=self.name, name=t.get("name", ""), exposed=exposed,
                    description=(t.get("description") or "")[:1000],
                    input_schema=t.get("inputSchema")
                    or {"type": "object", "properties": {}},
                ))
        except Exception as e:
            self.error = str(e)
            self.stop()

    def stop(self) -> None:
        if self.proc is not None:
            from .tools import _terminate_process_tree
            _terminate_process_tree(self.proc)
            self.proc = None
        # fail anything still waiting
        with self._lock:
            for entry in self._pending.values():
                entry["msg"] = {"error": {"message": "server stopped"}}
                entry["event"].set()
            self._pending.clear()

    # -- JSON-RPC over newline-delimited stdio -------------------------- #

    def _read_loop(self) -> None:
        proc = self.proc
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # servers sometimes log junk to stdout
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self._lock:
                        entry = self._pending.get(msg["id"])
                    if entry is not None:
                        entry["msg"] = msg
                        entry["event"].set()
                elif "id" in msg and "method" in msg:
                    # server -> client request (sampling etc.): politely decline
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601,
                                          "message": "not supported by this client"}})
                # notifications from the server: ignored
        except (ValueError, OSError):
            pass  # pipe closed -- server exiting

    def _send(self, msg: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("server is not running")
        data = json.dumps(msg, ensure_ascii=False)
        with self._lock:
            proc.stdin.write(data + "\n")
            proc.stdin.flush()

    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)

    def _rpc(self, method: str, params: dict, timeout: float):
        with self._lock:
            rid = self._next_id
            self._next_id += 1
            entry = {"event": threading.Event(), "msg": None}
            self._pending[rid] = entry
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params})
            # Wait in slices so a server that DIED fails in ~a second instead
            # of sitting out the whole timeout (a bad command would otherwise
            # stall startup for 45s).
            deadline = time.monotonic() + timeout
            while not entry["event"].wait(0.25):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
                if self.proc is None or self.proc.poll() is not None:
                    # tiny grace: the reader may still be draining its answer
                    if not entry["event"].wait(0.5):
                        raise RuntimeError("server exited "
                                           f"(code {self.proc.returncode if self.proc else '?'})")
                    break
            msg = entry["msg"]
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        if msg and "error" in msg:
            raise RuntimeError(msg["error"].get("message", str(msg["error"])))
        return (msg or {}).get("result", {})

    # -- tools ---------------------------------------------------------- #

    def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = self._rpc("tools/call",
                           {"name": tool_name, "arguments": arguments or {}},
                           timeout=CALL_TIMEOUT)
        parts = []
        for c in result.get("content", []):
            kind = c.get("type")
            if kind == "text":
                parts.append(c.get("text", ""))
            elif kind == "resource":
                r = c.get("resource", {})
                parts.append(r.get("text") or f"[resource: {r.get('uri', '?')}]")
            elif kind == "image":
                parts.append("[image content returned -- not displayable here]")
        out = "\n".join(p for p in parts if p) or "(no content returned)"
        if result.get("isError"):
            raise ToolErrorBase(out, ErrorSeverity.ERROR)
        return out


class McpManager:
    """All configured servers; the agent-facing surface."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.servers: dict[str, McpServer] = {}
        self._lock = threading.Lock()
        self._started = False

    def _config_entries(self) -> list[dict]:
        return [e for e in (getattr(self.cfg, "mcp_servers", None) or [])
                if e.get("name") and e.get("command")]

    def start_all(self) -> None:
        """Start every configured server (idempotent per config entry)."""
        with self._lock:
            entries = self._config_entries()
            wanted = {e["name"] for e in entries}
            # drop servers removed from config
            for name in [n for n in self.servers if n not in wanted]:
                self.servers.pop(name).stop()
            to_start = [e for e in entries
                        if e["name"] not in self.servers
                        or not self.servers[e["name"]].running]
        for e in to_start:
            srv = McpServer(e["name"], e["command"])
            srv.start()
            with self._lock:
                self.servers[e["name"]] = srv
        self._started = True

    def start_all_async(self) -> None:
        threading.Thread(target=self.start_all, daemon=True,
                         name="mcp-startup").start()

    def restart(self, name: str) -> None:
        with self._lock:
            srv = self.servers.pop(name, None)
        if srv:
            srv.stop()
        entry = next((e for e in self._config_entries() if e["name"] == name), None)
        if entry:
            srv = McpServer(entry["name"], entry["command"])
            srv.start()
            with self._lock:
                self.servers[name] = srv

    def stop_all(self) -> None:
        with self._lock:
            servers = list(self.servers.values())
            self.servers.clear()
        for s in servers:
            s.stop()

    # -- agent surface --------------------------------------------------- #

    def _tool_map(self) -> dict[str, McpTool]:
        out = {}
        with self._lock:
            for srv in self.servers.values():
                if srv.running:
                    for t in srv.tools:
                        out[t.exposed] = t
        return out

    def tool_schemas(self) -> list[dict]:
        schemas = []
        for t in self._tool_map().values():
            schemas.append({"type": "function", "function": {
                "name": t.exposed,
                "description": f"[{t.server} MCP server] {t.description}",
                "parameters": t.input_schema,
            }})
        return schemas

    def owns(self, name: str) -> bool:
        return name.startswith("mcp_") and name in self._tool_map()

    def call(self, name: str, args: dict) -> str:
        t = self._tool_map().get(name)
        if t is None:
            raise ToolErrorBase(f"unknown MCP tool: {name}", ErrorSeverity.ERROR)
        with self._lock:
            srv = self.servers.get(t.server)
        if srv is None or not srv.running:
            raise ToolErrorBase(
                f"MCP server '{t.server}' is not running", ErrorSeverity.ERROR)
        try:
            return srv.call_tool(t.name, args)
        except ToolErrorBase:
            raise
        except Exception as e:
            raise ToolErrorBase(f"MCP call failed: {e}", ErrorSeverity.ERROR)

    # -- status (settings UI) -------------------------------------------- #

    def status(self) -> list[dict]:
        entries = self._config_entries()
        out = []
        with self._lock:
            servers = dict(self.servers)
        for e in entries:
            srv = servers.get(e["name"])
            out.append({
                "name": e["name"], "command": e["command"],
                "running": bool(srv and srv.running),
                "tools": [t.exposed for t in (srv.tools if srv else [])],
                "error": (srv.error if srv else "") or "",
            })
        return out
