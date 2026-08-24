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
import sys
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
# How long the extension's own keepalive may be missing before its service
# worker is presumed gone. It speaks every 20 seconds (KEEPALIVE_MS in
# background.js), so this is two missed beats and a margin -- long enough that
# an ordinary hiccup is not called a death, short enough that the extension's
# next wake re-dials rather than the user sitting in front of a Settings panel
# that says Connected while nothing works.
WORKER_QUIET_AFTER = 50.0
# A screenshot is the only big thing that crosses this wire; the cap is a
# sanity bound, not a budget.
MAX_MESSAGE = 32 * 1024 * 1024
# How often the heartbeat wakes to decide anything.
HEARTBEAT_TICK = 0.5
# How long a call waits for the extension to dial back in before giving up. The
# socket blinks for ordinary reasons -- Chrome recycling the service worker, an
# extension reload, a browser restart -- and each is well under a second.
RECONNECT_GRACE = 6.0

# Commands that can be repeated on a fresh socket without doing anything twice.
# A call whose connection dropped might or might not have run; for a click or a
# form fill that is not a question worth guessing at, so only reads are retried.
SAFE_TO_REPEAT = frozenset({
    "status", "tabs", "info", "snapshot", "text", "exists", "enabled",
    "screenshot",
})

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class BridgeError(RuntimeError):
    """The extension is not connected, or did not answer."""


class _Blinked(Exception):
    """The CONNECTION went away, as opposed to the call failing.

    Internal: never reaches a caller. It is the difference between "the browser
    said no" and "the wire moved", and only the second one is worth retrying.
    """


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


def read_frame(sock) -> tuple[int, bytes, bool]:
    """One frame off the wire, unmasked. Returns (opcode, payload, fin).

    Client frames are ALWAYS masked -- the spec requires it, and a server that
    ignored the mask bit would read scrambled bytes rather than fail, which is
    the kind of bug that looks like the other end being broken.

    FIN is returned rather than ignored because a big message arrives in
    PIECES: RFC 6455 §5.4 lets a sender fragment, and Chrome does it for
    anything large. See read_message.
    """
    b0, b1 = _read_exactly(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _read_exactly(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _read_exactly(sock, 8))[0]
    if n > MAX_MESSAGE:
        raise ConnectionError("frame too large")
    mask = _read_exactly(sock, 4) if masked else b""
    data = _read_exactly(sock, n) if n else b""
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return opcode, data, fin


def read_message(sock, on_control) -> tuple[int, bytes]:
    """One whole MESSAGE, reassembled from however many frames it took.

    This is what a screenshot needs. Every other command in this protocol
    answers in a few hundred bytes and arrives in a single frame, so the
    missing reassembly went unnoticed until the browser agent took its first
    screenshot -- a multi-megabyte data URL, which Chrome sends fragmented.
    The first frame carried a fraction of the JSON (unparseable, dropped) and
    every continuation frame had opcode 0, which the read loop skipped as "not
    text". The reply simply vanished, and the task died waiting for it.

    Control frames (ping/pong/close) may be interleaved between fragments and
    are handed to `on_control` rather than joined into the payload.
    """
    opcode, data, fin = read_frame(sock)
    while opcode in (OP_PING, OP_PONG, OP_CLOSE):
        on_control(opcode, data)
        if opcode == OP_CLOSE:
            return OP_CLOSE, b""
        opcode, data, fin = read_frame(sock)
    chunks = [data]
    total = len(data)
    while not fin:
        cont, more, fin = read_frame(sock)
        if cont in (OP_PING, OP_PONG, OP_CLOSE):
            on_control(cont, more)
            if cont == OP_CLOSE:
                return OP_CLOSE, b""
            fin = False                      # still mid-message
            continue
        if cont != OP_CONT:
            raise ConnectionError(
                f"expected a continuation frame, got opcode {cont}")
        chunks.append(more)
        total += len(more)
        if total > MAX_MESSAGE:
            raise ConnectionError("message too large")
    return opcode, b"".join(chunks)


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
        self._last_seen = 0.0          # any frame, a protocol pong included
        # When the extension last spoke AS THE EXTENSION -- a hello, a
        # keepalive, or a reply. Kept apart from _last_seen because they are
        # evidence of different things; see `connected`.
        self._last_message = 0.0
        # Whether THIS connection has ever sent a keepalive. An extension
        # predating them (they are loaded unpacked, so an old copy is a real
        # possibility) would otherwise be declared dead every 50 seconds for
        # not speaking a language it does not know.
        self._keepalive_seen = False
        self._reconnect_grace = RECONNECT_GRACE
        # The last few connect/disconnect events, with reasons. The one thing
        # every report of this feature has needed and none could supply: when
        # the extension went, and what the app thought happened.
        self.events: list[dict] = []

    # -- lifecycle ---------------------------------------------------- #

    def start(self) -> int:
        """Bind and start accepting. Returns the port. Idempotent."""
        if self._srv is not None:
            return self.port
        last = None
        for port in self.ports:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # SO_REUSEADDR means two different things. On Unix it only permits
            # rebinding a port left in TIME_WAIT, which is what is wanted here.
            # On WINDOWS it permits a second socket to bind a port that is
            # ALREADY IN ACTIVE USE, and which of the two then receives
            # connections is not defined -- so a second copy of this app (or
            # anything else) can silently take the port from under a running
            # bridge, and the extension's connections start being refused
            # while the first app still believes it owns the socket.
            #
            # SO_EXCLUSIVEADDRUSE is the Windows option that means what
            # SO_REUSEADDR means everywhere else: nobody else gets this port.
            if sys.platform == "win32":
                try:
                    srv.setsockopt(socket.SOL_SOCKET,
                                   socket.SO_EXCLUSIVEADDRUSE, 1)
                except (AttributeError, OSError):
                    pass
            else:
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
        """A socket, and recent proof the EXTENSION -- not the socket -- is
        still there.

        Holding an open socket is not evidence of a live browser: a laptop that
        slept, a browser that was killed, a FIN that never arrived all leave a
        socket that reads as fine and answers nothing. The bridge pings for
        that reason, and for a while this asked when any frame last arrived.

        That is still not enough, and background.js says why in its own
        comments: **a protocol-level pong is answered by Chrome's network stack
        without the service worker being woken at all.** The worker is what
        runs `tabs`, `snapshot`, `click` -- everything. So a reaped worker left
        a socket that answered every ping and no command, and the app called it
        Connected while each action sat out its full timeout and failed with
        "the browser did not answer in time". That is the exact report this
        replaces, and it is the same lesson one level up: an ANSWERING socket
        is not evidence of a live service worker.

        The extension therefore speaks from JavaScript on a timer, and this
        counts only what the extension itself said -- a hello, a keepalive, or
        a reply. A connection that never sends keepalives is an older build
        that does not know how; it keeps the old any-frame rule rather than
        being declared dead for speaking an older language.
        """
        if self._client is None:
            return False
        if self._keepalive_seen:
            return (time.monotonic() - self._last_message) < WORKER_QUIET_AFTER
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
            if self._keepalive_seen and \
                    (now - self._last_message) >= WORKER_QUIET_AFTER:
                # Still ponging, but the extension itself has gone quiet: its
                # service worker has been reaped and the socket outlived it.
                # Dropping is the RECOVERY, not just the bookkeeping -- the
                # extension re-dials the moment it next wakes (its reconnect
                # alarm, or the user touching a tab), and a fresh connection
                # replaces this one. Holding the corpse instead is what left
                # Settings saying Connected while every command timed out.
                self._drop(conn, "the extension stopped answering "
                                 "(its service worker was probably reaped)")
                continue
            if (now - last_ping) < PING_EVERY:
                continue
            last_ping = now
            try:
                with self._send_lock:
                    conn.sendall(encode_frame(b"", OP_PING))
            except OSError:
                self._drop(conn, "the connection broke")

    def _log(self, what: str, why: str = "") -> None:
        self.events.append({"at": time.time(), "what": what, "why": why})
        del self.events[:-12]

    def _drop(self, conn, why: str) -> None:
        with self._client_lock:
            if self._client is not conn:
                return
            self._client = None
        self._log("dropped", why)
        try:
            conn.close()
        except OSError:
            pass
        with self._pending_lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for q in waiting:
            q.put({"gone": True, "why": why})
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
            self._last_seen = self._last_message = time.monotonic()
            # Per connection, not per bridge: the flag says what THIS extension
            # speaks, and a browser restart may bring a different build.
            self._keepalive_seen = False
            self._log("connected", "replacing an earlier connection"
                      if self._client is not None else "")
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
        closed_by = ""
        try:
            while True:
                def control(op, payload, _conn=conn):
                    # ANY frame is proof the other end is alive, and a pong is
                    # the only one that arrives on a quiet connection. Marking
                    # liveness only when a whole message lands would let a
                    # healthy but idle browser go stale and be dropped.
                    self._last_seen = time.monotonic()
                    if op == OP_PING:
                        with self._send_lock:
                            _conn.sendall(encode_frame(payload, OP_PONG))

                opcode, data = read_message(conn, control)
                self._last_seen = time.monotonic()
                if opcode == OP_TEXT:
                    # The extension speaking, rather than its browser's network
                    # stack answering for it. This is the only thing that
                    # proves the service worker is alive to run a command.
                    self._last_message = self._last_seen
                if opcode == OP_CLOSE:
                    closed_by = "the browser closed the connection"
                    break
                if opcode != OP_TEXT:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._dispatch(msg)
        except ConnectionError as e:
            # Our own read giving up -- a malformed or oversized stream. Said
            # as such: reporting it as "the browser closed the connection"
            # once cost a whole round of looking in the wrong place.
            closed_by = f"gave up reading the connection ({e})"
        except (OSError, struct.error) as e:
            closed_by = f"the connection broke ({type(e).__name__})"
        finally:
            with self._client_lock:
                was_current = self._client is conn
                if was_current:
                    self._client = None
            if was_current:
                self._log("dropped", closed_by or "the browser closed the connection")
            try:
                conn.close()
            except OSError:
                pass
            # Anything still waiting would otherwise block for the full
            # timeout on a browser that has already gone away.
            with self._pending_lock:
                waiting = list(self._pending.values())
            for q in waiting:
                q.put({"gone": True})
            self._notify()

    def _dispatch(self, msg: dict) -> None:
        if msg.get("type") == "keepalive":
            # No id and nothing to answer: receiving it IS the point. It also
            # says this extension is new enough to be held to the
            # worker-liveness rule in `connected`.
            self._keepalive_seen = True
            return
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

    def _wait_for_client(self):
        """The live socket, waiting briefly for a reconnect if there isn't one.

        An extension's socket does not stay up forever, and it is not supposed
        to: Chrome recycles the service worker, the extension is reloaded, the
        browser restarts. Each of those is a gap of well under a second before
        it dials back in. Failing a browser action into that gap is what
        produced "it switched tab, then said the extension isn't connected" --
        a task dying because the transport blinked, which is not a thing the
        model, or the user, can do anything about.

        So a call that arrives during a blink waits for it to end. Only a
        browser that is genuinely gone -- closed, or the extension removed --
        runs the clock out, and that one gets the message about it.
        """
        conn = self._client
        if conn is not None:
            return conn
        if self._reconnect_grace > 0 and not self._never_connected():
            deadline = time.monotonic() + self._reconnect_grace
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.05)
                conn = self._client
                if conn is not None:
                    return conn
        raise BridgeError(
            "The browser extension isn't connected. Open the browser you "
            "installed it in, or check Settings -> Browser." if self._never_connected()
            else "The browser extension dropped and did not come back. Is that "
                 "browser still open, and is its toolbar button showing a pause "
                 "mark?")

    def _never_connected(self) -> bool:
        return self._last_seen == 0.0

    def call(self, command: str, timeout: float | None = None, **params):
        """Ask the extension to do one thing and wait for its answer.

        Raises BridgeError when nothing is connected, when the browser goes
        away mid-call, or when the extension reports a failure -- callers turn
        that into the same BrowserError any other backend would raise.
        """
        wait = timeout if timeout is not None else self.timeout
        try:
            return self._once(command, params, wait)
        except _Blinked:
            # The socket went away with the call in flight. For a read that is
            # nothing but bad luck, and repeating it on the new connection is
            # exactly what the caller would do by hand.
            if command not in SAFE_TO_REPEAT:
                raise BridgeError(
                    f"The browser extension disconnected while '{command}' was "
                    "running, so I can't tell whether it happened. Look at the "
                    "page before repeating it.")
            try:
                return self._once(command, params, wait)
            except _Blinked:
                raise BridgeError(
                    "The browser extension keeps dropping the connection. Is "
                    "that browser still open?")

    def _once(self, command: str, params: dict, wait: float):
        """One attempt. Raises _Blinked if the connection went, not the call."""
        conn = self._wait_for_client()
        rid = uuid.uuid4().hex
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = q
        payload = json.dumps({"id": rid, "command": command, "params": params})
        try:
            with self._send_lock:
                conn.sendall(encode_frame(payload.encode("utf-8")))
        except OSError:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise _Blinked()
        try:
            reply = q.get(timeout=wait)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            # Name the likely cause. "did not answer in time" on its own sent
            # every report of this to the wrong place -- the user looks at
            # Settings, sees Connected, and concludes the app is lying to
            # them. It was, and now it does not; but the message still has to
            # say what this actually means and that it recovers by itself.
            raise BridgeError(
                f"The browser did not answer '{command}' in time. Its extension "
                "is installed and the socket is open, but the service worker "
                "behind it is not responding -- Chrome reaps those when idle. "
                "It re-dials within about half a minute; try again after that, "
                "or click any tab in that browser to wake it now.")
        if reply.get("gone"):
            raise _Blinked()
        if reply.get("error"):
            raise BridgeError(str(reply["error"]))
        return reply.get("result")
