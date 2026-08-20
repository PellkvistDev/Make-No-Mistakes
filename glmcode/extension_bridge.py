"""A localhost WebSocket server the browser extension talks to.

Why this exists at all: Chrome's DevTools port can only be opened at LAUNCH.
There is no way to switch it on for a browser that is already running, so
"attach to the window I have open" is impossible from the outside -- the honest
version of it was always "quit your browser, reopen it with these flags", which
is not a feature anyone wants to use twice.

An extension is inside the browser already. It has the user's profile, their
logins and their open tabs, and it needs no flags, no relaunch and no second
profile directory. So the direction of the connection flips: the app stops
trying to reach into the browser, and the browser reaches out to the app.

WebSocket rather than polling, for a specific reason: an MV3 service worker is
killed after 30 seconds idle, and WebSocket activity is the documented thing
that resets that timer. A fetch loop would have the worker dying underneath it.

Hand-rolled rather than a dependency, because the server half of RFC 6455 that
this needs is small and completely specified: a SHA-1 handshake, and text
frames that are always masked coming from a client. The phone app's whole
design is "a folder of static files with no toolchain to rot", and adding a
package to this project to speak a protocol it can speak in 200 lines would be
the wrong trade.

SECURITY -- the important part. A WebSocket is not subject to CORS, so any web
page in any tab can open a socket to 127.0.0.1 and start talking. The boundary
is the Origin header, which the BROWSER sets and page JavaScript cannot forge:
an extension's service worker sends `chrome-extension://<id>`, and a page sends
its own https origin. Only the former is accepted, and the socket is bound to
loopback so nothing off this machine can reach it at all.
"""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
import uuid

# RFC 6455 §1.3. Concatenated with the client's key, SHA-1'd, base64'd.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# The app tries these in order and the extension does the same, so a port
# already taken by something else costs a second of startup rather than the
# feature. Keep the two lists identical -- extension/background.js.
PORTS = (8765, 8766, 8767, 8768, 8769)

# Long enough for a slow page load behind a browser action, short enough that a
# browser which has genuinely gone away is not sat behind for half a minute.
DEFAULT_TIMEOUT = 20.0
# How often the accept loop wakes to notice it has been told to stop.
ACCEPT_POLL = 0.25
# Heartbeat. Browsers answer a WebSocket ping at the protocol level, without
# the service worker being woken, so this keeps telling the truth even while
# Chrome has the worker asleep.
PING_EVERY = 8.0
STALE_AFTER = 30.0
# How often the heartbeat wakes to decide anything.
HEARTBEAT_TICK = 0.5

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class BridgeError(RuntimeError):
    """The extension is not connected, or did not answer."""


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _read_exactly(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """One unmasked server frame. Servers must NOT mask (RFC 6455 §5.1)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def read_frame(sock) -> tuple[int, bytes]:
    """One frame off the wire, unmasking it. Returns (opcode, payload).

    Client frames are ALWAYS masked -- the spec requires it, and a server that
    ignored the mask bit would read scrambled bytes rather than fail, which is
    the kind of bug that looks like the other end being broken.
    """
    b0, b1 = _read_exactly(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _read_exactly(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _read_exactly(sock, 8))[0]
    if n > 32 * 1024 * 1024:
        raise ConnectionError("frame too large")
    mask = _read_exactly(sock, 4) if masked else b""
    data = _read_exactly(sock, n) if n else b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data


def origin_is_extension(origin: str) -> bool:
    """The whole security boundary, in one line.

    A page cannot set its own Origin header -- the browser does -- so a
    malicious site that opens a socket here announces itself as https://... and
    is refused. Any extension is accepted rather than one pinned ID, because an
    unpacked extension's ID changes with its path, and telling someone their
    own extension is the wrong one is a worse failure than the one this
    prevents. Nothing off this machine can reach the port regardless.
    """
    return (origin or "").startswith("chrome-extension://")


class ExtensionBridge:
    """Accepts one extension connection and does request/response over it."""

    def __init__(self, ports=PORTS, timeout: float = DEFAULT_TIMEOUT):
        self.ports = tuple(ports)
        self.timeout = timeout
        self.port: int | None = None
        self._srv: socket.socket | None = None
        self._client: socket.socket | None = None
        self._client_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_change = None          # called when a client connects/leaves
        self.hello: dict = {}          # what the extension said about itself
        self._last_seen = 0.0          # when the extension last said anything

    # -- lifecycle ---------------------------------------------------- #

    def start(self) -> int:
        """Bind and start accepting. Returns the port. Idempotent."""
        if self._srv is not None:
            return self.port
        last = None
        for port in self.ports:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind(("127.0.0.1", port))       # loopback only, always
                srv.listen(1)
            except OSError as e:
                srv.close()
                last = e
                continue
            # The accept loop wakes on this to check _stop. Closing a socket
            # another thread is blocked in accept() on does NOT release the
            # port on Linux -- the close waits for the blocked call, which
            # never returns, and the port stays held for the life of the
            # process. A timeout is the portable way out of that.
            srv.settimeout(ACCEPT_POLL)
            # port 0 asks the OS for a free one; ask the socket what it got.
            self._srv, self.port = srv, srv.getsockname()[1]
            break
        if self._srv is None:
            raise BridgeError(f"No free port in {self.ports}: {last}")
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        return self.port

    def stop(self) -> None:
        """Stop accepting and release the port.

        The join is not tidiness. Closing a listening socket while another
        thread sits in accept() does not release the port -- the close waits on
        the blocked call. So the accept loop is told to stop, given its half
        second to notice, and only then is the socket closed. Without this, a
        stopped bridge holds its port for the life of the process and the next
        one walks down the list to the next port.
        """
        self._stop.set()
        try:
            if self._client:
                self._client.close()
        except OSError:
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=ACCEPT_POLL * 4)
        try:
            if self._srv:
                self._srv.close()
        except OSError:
            pass
        self._client = self._srv = self._thread = None

    @property
    def connected(self) -> bool:
        """A socket AND recent proof the other end is still there.

        Holding an open socket is not evidence of a live browser. A laptop that
        slept, a browser that was killed, a network stack that never delivered
        the FIN -- all leave a socket that reads as fine and answers nothing.
        Reported as connected, that produced the worst version of this feature:
        Settings said "Connected", and every browser action then sat for the
        full timeout before failing.

        So the bridge pings, and this asks when the extension was last heard
        from rather than whether a file descriptor exists.
        """
        if self._client is None:
            return False
        return (time.monotonic() - self._last_seen) < STALE_AFTER

    def _heartbeat_loop(self) -> None:
        """Ping the extension, and drop it when it stops answering.

        Browsers answer a WebSocket ping at the protocol level, without the
        page or the service worker being involved, so this stays true even
        while the worker is asleep -- which is exactly when we must not decide
        the extension has gone away.
        """
        # Ticks finely and decides from elapsed time, rather than sleeping for
        # the whole interval: a loop parked in an 8-second wait cannot notice a
        # shutdown, and cannot react at all to a bridge configured with shorter
        # timings.
        last_ping = 0.0
        while not self._stop.is_set():
            self._stop.wait(HEARTBEAT_TICK)
            if self._stop.is_set():
                return
            conn = self._client
            if conn is None:
                continue
            now = time.monotonic()
            if (now - self._last_seen) >= STALE_AFTER:
                # Answered nothing for long enough that the socket is a fiction.
                self._drop(conn, "stopped answering")
                continue
            if (now - last_ping) < PING_EVERY:
                continue
            last_ping = now
            try:
                with self._send_lock:
                    conn.sendall(encode_frame(b"", OP_PING))
            except OSError:
                self._drop(conn, "the connection broke")

    def _drop(self, conn, why: str) -> None:
        with self._client_lock:
            if self._client is not conn:
                return
            self._client = None
        try:
            conn.close()
        except OSError:
            pass
        with self._pending_lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for q in waiting:
            q.put({"error": f"the browser extension {why}"})
        self._notify()

    # -- server ------------------------------------------------------- #

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(None)
            try:
                if not self._handshake(conn):
                    conn.close()
                    continue
            except (OSError, ConnectionError, UnicodeDecodeError):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            # One extension at a time. A second connection replaces the first,
            # which is what a browser restart or an extension reload looks
            # like from here -- refusing it would need a manual reconnect.
            self._last_seen = time.monotonic()
            with self._client_lock:
                old, self._client = self._client, conn
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            self._notify()
            threading.Thread(target=self._read_loop, args=(conn,),
                             daemon=True).start()

    def _handshake(self, conn) -> bool:
        conn.settimeout(10)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            raw += chunk
            if len(raw) > 16384:
                return False
        head = raw.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        lines = head.split("\r\n")
        headers = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        key = headers.get("sec-websocket-key", "")
        if not key or "websocket" not in headers.get("upgrade", "").lower():
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return False
        if not origin_is_extension(headers.get("origin", "")):
            # Said out loud rather than dropped: this is the one refusal an
            # honest client can hit, and silence would read as "the app isn't
            # running".
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n"
                         b"only a browser extension may connect")
            return False
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + _accept_key(key).encode("ascii") +
            b"\r\n\r\n")
        conn.settimeout(None)
        return True

    def _read_loop(self, conn) -> None:
        try:
            while True:
                opcode, data = read_frame(conn)
                self._last_seen = time.monotonic()
                if opcode == OP_CLOSE:
                    break
                if opcode == OP_PING:
                    self._send_raw(conn, encode_frame(data, OP_PONG))
                    continue
                if opcode != OP_TEXT:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._dispatch(msg)
        except (OSError, ConnectionError, struct.error):
            pass
        finally:
            with self._client_lock:
                if self._client is conn:
                    self._client = None
            try:
                conn.close()
            except OSError:
                pass
            # Anything still waiting would otherwise block for the full
            # timeout on a browser that has already gone away.
            with self._pending_lock:
                waiting = list(self._pending.values())
            for q in waiting:
                q.put({"error": "the browser extension disconnected"})
            self._notify()

    def _dispatch(self, msg: dict) -> None:
        if msg.get("type") == "hello":
            self.hello = {k: v for k, v in msg.items() if k != "type"}
            self._notify()
            return
        rid = msg.get("id")
        if not rid:
            return
        with self._pending_lock:
            q = self._pending.pop(rid, None)
        if q is not None:
            q.put(msg)

    def _notify(self) -> None:
        cb = self.on_change
        if cb:
            try:
                cb(self.connected)
            except Exception:
                pass

    @staticmethod
    def _send_raw(conn, frame: bytes) -> None:
        conn.sendall(frame)

    # -- request/response --------------------------------------------- #

    def call(self, command: str, timeout: float | None = None, **params):
        """Ask the extension to do one thing and wait for its answer.

        Raises BridgeError when nothing is connected, when the browser goes
        away mid-call, or when the extension reports a failure -- callers turn
        that into the same BrowserError any other backend would raise.
        """
        conn = self._client
        if conn is None:
            raise BridgeError(
                "The browser extension isn't connected. Open the browser you "
                "installed it in, or check Settings -> Browser.")
        rid = uuid.uuid4().hex
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = q
        payload = json.dumps({"id": rid, "command": command, "params": params})
        try:
            with self._send_lock:
                conn.sendall(encode_frame(payload.encode("utf-8")))
        except OSError as e:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BridgeError(f"Could not reach the extension: {e}")
        try:
            reply = q.get(timeout=timeout if timeout is not None else self.timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BridgeError(f"The browser did not answer '{command}' in time.")
        if reply.get("error"):
            raise BridgeError(str(reply["error"]))
        return reply.get("result")
