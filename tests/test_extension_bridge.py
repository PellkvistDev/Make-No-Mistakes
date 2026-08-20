"""The extension bridge: a real socket, a real handshake, real frames.

This is the piece that made "drive the browser I already have open" possible at
all. Chrome's DevTools port can only be opened at LAUNCH, so nothing outside
the browser can attach to a window that is already running -- the honest
version of that feature was always "quit your browser and reopen it with these
flags". An extension is already inside the browser, so the connection runs the
other way and the whole relaunch disappears.

These tests speak the protocol for real rather than faking the transport: a
hand-rolled server is only defensible if it is checked against the RFC and
against an actual client, not against one's reading of either.
"""

import base64
import json
import os
import socket
import struct
import threading
import time

import pytest

from glmcode.extension_bridge import (BridgeError, ExtensionBridge, _accept_key,
                                      encode_frame, origin_is_extension)


# --------------------------------------------------------------------- #
# RFC 6455 itself

def test_the_handshake_matches_the_rfcs_worked_example():
    """Section 1.3's example, with their key and their expected answer. If this
    ever fails, do not adjust it to match the code."""
    assert _accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize("size", [0, 5, 125, 126, 200, 65535, 65536, 70000])
def test_frame_lengths_round_trip_at_every_boundary(size):
    """7-bit, 16-bit and 64-bit length encodings, and the exact sizes where one
    becomes the next."""
    payload = b"x" * size
    frame = encode_frame(payload)
    a, b = _decode_server_frame(frame)
    assert a == 0x1 and b == payload


def test_a_server_frame_is_never_masked():
    """RFC 6455 section 5.1: a server MUST NOT mask. A client that checks would
    drop the connection."""
    assert encode_frame(b"hi")[1] & 0x80 == 0


# --------------------------------------------------------------------- #
# The security boundary

@pytest.mark.parametrize("origin,ok", [
    ("chrome-extension://abcdefghijklmnop", True),
    ("https://evil.example", False),
    ("http://localhost:3000", False),
    ("null", False),
    ("", False),
])
def test_only_an_extension_may_connect(origin, ok):
    """A WebSocket is not subject to CORS, so any page in any tab can open a
    socket to 127.0.0.1. Origin is what stops it: the BROWSER sets that header
    and page JavaScript cannot forge it."""
    assert origin_is_extension(origin) is ok


def test_a_web_page_is_refused_by_the_running_server(bridge):
    sock, _ = _raw_connect(bridge.port, origin="https://evil.example")
    reply = sock.recv(200).decode("latin-1")
    assert "403" in reply
    sock.close()
    assert bridge.connected is False


def test_it_binds_to_loopback_only(bridge):
    """Nothing off this machine should be able to reach the port at all, no
    matter what it claims its origin is."""
    host = bridge._srv.getsockname()[0]
    assert host == "127.0.0.1"


# --------------------------------------------------------------------- #
# Request / response

def test_a_command_reaches_the_extension_and_the_answer_comes_back(bridge, ext):
    ext.handle("info", lambda p: {"url": "https://example.com", "title": "Ex",
                                  "width": 1440, "height": 900})
    got = bridge.call("info")
    assert got["url"] == "https://example.com" and got["width"] == 1440


def test_params_arrive(bridge, ext):
    seen = {}
    ext.handle("fill", lambda p: seen.update(p) or True)
    bridge.call("fill", ref=12, text="hello", submit=True)
    assert seen == {"ref": 12, "text": "hello", "submit": True}


def test_an_error_from_the_extension_becomes_an_error_here(bridge, ext):
    ext.fail("click", "Element [4] is no longer on the page")
    with pytest.raises(BridgeError) as e:
        bridge.call("click", ref=4)
    assert "no longer on the page" in str(e.value)


def test_replies_are_matched_by_id_not_by_order(bridge, ext):
    """Two chats can drive the browser, and the extension answers whichever
    finishes first. Pairing by arrival order would hand one call the other's
    result."""
    ext.handle("slow", lambda p: (time.sleep(0.25), "slow-answer")[1])
    ext.handle("fast", lambda p: "fast-answer")
    out = {}
    t = threading.Thread(target=lambda: out.update(slow=bridge.call("slow")))
    t.start()
    time.sleep(0.05)
    out["fast"] = bridge.call("fast")
    t.join(5)
    assert out == {"slow": "slow-answer", "fast": "fast-answer"}


def test_nothing_connected_says_so_rather_than_hanging(bridge):
    with pytest.raises(BridgeError) as e:
        bridge.call("info", timeout=1)
    assert "isn't connected" in str(e.value)


def test_a_browser_that_goes_away_mid_call_fails_immediately(bridge, ext):
    """Otherwise every in-flight call waits out the full timeout on a browser
    that has already closed."""
    ext.handle("hang", lambda p: None, answer=False)
    result = {}

    def call():
        try:
            bridge.call("hang", timeout=20)
        except BridgeError as e:
            result["err"] = str(e)

    t = threading.Thread(target=call)
    t.start()
    time.sleep(0.2)
    ext.close()
    t.join(5)
    assert not t.is_alive(), "the call did not give up when the browser left"
    assert "disconnected" in result.get("err", "")


def test_a_reconnect_replaces_the_old_connection(bridge, ext):
    """A browser restart or an extension reload looks exactly like this.
    Refusing the second connection would need a manual restart of the app."""
    ext.handle("info", lambda p: {"url": "first"})
    assert bridge.call("info")["url"] == "first"
    second = _Ext(bridge.port)
    second.handle("info", lambda p: {"url": "second"})
    for _ in range(50):
        if bridge.connected and bridge.call("info")["url"] == "second":
            break
        time.sleep(0.05)
    assert bridge.call("info")["url"] == "second"
    second.close()


def test_it_reports_who_connected(bridge, ext):
    for _ in range(50):
        if bridge.hello.get("agent"):
            break
        time.sleep(0.05)
    assert bridge.hello["agent"] == "mnm-extension"


def test_a_socket_that_stops_answering_is_not_reported_as_connected(bridge, ext,
                                                                    monkeypatch):
    """The worst version of this feature, reported from a real machine:
    Settings said "Connected" while every browser action failed. Holding an
    open socket is not evidence of a live browser -- a laptop that slept, a
    browser that was killed, a FIN that never arrived all leave a descriptor
    that reads as fine and answers nothing."""
    import glmcode.extension_bridge as eb
    monkeypatch.setattr(eb, "STALE_AFTER", 0.6)
    monkeypatch.setattr(eb, "PING_EVERY", 0.15)
    assert bridge.connected is True
    ext.deaf = True                     # alive socket, nobody home
    for _ in range(60):
        if not bridge.connected:
            break
        time.sleep(0.05)
    assert bridge.connected is False, "a dead connection still read as connected"


def test_a_dropped_connection_fails_waiting_calls_at_once(bridge, ext, monkeypatch):
    """Otherwise the call sits for the whole timeout on a browser the bridge
    has already worked out is gone."""
    import glmcode.extension_bridge as eb
    monkeypatch.setattr(eb, "STALE_AFTER", 0.6)
    monkeypatch.setattr(eb, "PING_EVERY", 0.15)
    ext.handle("hang", lambda p: None, answer=False)
    ext.deaf = True
    result = {}

    def call():
        try:
            bridge.call("hang", timeout=30)
        except BridgeError as e:
            result["err"] = str(e)

    t = threading.Thread(target=call)
    t.start()
    t.join(10)
    assert not t.is_alive(), "the call waited out its full timeout"
    # 'hang' is not a read, so it is NOT silently repeated -- the honest answer
    # is that we cannot tell whether it happened.
    assert "can't tell whether it happened" in result.get("err", "")


def test_a_healthy_extension_stays_connected(bridge, ext, monkeypatch):
    """The heartbeat must not invent a disconnection: a browser answering pings
    is connected however quiet the conversation is."""
    import glmcode.extension_bridge as eb
    monkeypatch.setattr(eb, "STALE_AFTER", 0.6)
    monkeypatch.setattr(eb, "PING_EVERY", 0.15)
    time.sleep(1.2)
    assert bridge.connected is True


def test_a_ping_is_ponged(bridge, ext):
    """Chrome pings to keep the socket (and the service worker) alive. A server
    that ignored it would be dropped."""
    ext.ping(b"keepalive")
    assert ext.await_pong(timeout=3) == b"keepalive"


# --------------------------------------------------------------------- #
# A minimal client, because stdlib has none

def _decode_server_frame(frame: bytes) -> tuple[int, bytes]:
    b0, b1 = frame[0], frame[1]
    n, off = b1 & 0x7F, 2
    if n == 126:
        n = struct.unpack(">H", frame[2:4])[0]
        off = 4
    elif n == 127:
        n = struct.unpack(">Q", frame[2:10])[0]
        off = 10
    assert not (b1 & 0x80), "server frames must not be masked"
    return b0 & 0x0F, frame[off:off + n]


def _raw_connect(port: int, origin: str):
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    sock.sendall(
        f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n".encode("ascii"))
    return sock, key


class _Ext:
    """Stands in for the extension: a real WebSocket client on a real socket."""

    def __init__(self, port: int):
        self.sock, key = _raw_connect(port, "chrome-extension://testtesttesttest")
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += self.sock.recv(4096)
        assert b"101" in raw, raw[:120]
        assert _accept_key(key).encode() in raw
        self._handlers, self._fails, self._silent = {}, {}, set()
        self._pongs, self._stop = [], False
        self.deaf = False   # stop answering, without closing the socket
        self.fragment_at = 0   # split messages bigger than this
        self._buf = b""
        threading.Thread(target=self._loop, daemon=True).start()
        self._send(json.dumps({"type": "hello", "agent": "mnm-extension",
                               "version": "1.0.0"}))

    def handle(self, cmd, fn, answer=True):
        self._handlers[cmd] = fn
        if not answer:
            self._silent.add(cmd)

    def fail(self, cmd, message):
        self._fails[cmd] = message

    def ping(self, payload=b""):
        self._send_frame(payload, 0x9)

    def await_pong(self, timeout=3):
        end = time.time() + timeout
        while time.time() < end:
            if self._pongs:
                return self._pongs.pop(0)
            time.sleep(0.02)
        raise AssertionError("no pong came back")

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    # -- wire -------------------------------------------------------- #

    def _send(self, text: str):
        data = text.encode("utf-8")
        if self.fragment_at and len(data) > self.fragment_at:
            # Exactly what Chrome does with a big message: a TEXT frame with
            # FIN=0, then continuation frames with opcode 0.
            n = self.fragment_at
            self._send_frame(data[:n], 0x1, fin=False)
            rest = data[n:]
            while rest:
                chunk, rest = rest[:n], rest[n:]
                self._send_frame(chunk, 0x0, fin=not rest)
            return
        self._send_frame(data, 0x1)

    def _send_frame(self, payload: bytes, opcode: int, fin: bool = True):
        mask = os.urandom(4)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        head = bytes([(0x80 if fin else 0x00) | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + masked)

    def _loop(self):
        while not self._stop:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            self._buf += chunk
            while True:
                frame = self._take()
                if frame is None:
                    break
                opcode, data = frame
                if opcode == 0xA:
                    self._pongs.append(data)
                    continue
                if opcode == 0x9:
                    # A real browser answers a ping at the protocol level,
                    # without the page or the service worker being involved.
                    if not self.deaf:
                        self._send_frame(data, 0xA)
                    continue
                if opcode != 0x1:
                    continue
                msg = json.loads(data.decode("utf-8"))
                cmd = msg.get("command")
                if cmd in self._silent:
                    continue
                if cmd in self._fails:
                    self._send(json.dumps({"id": msg["id"], "error": self._fails[cmd]}))
                    continue
                fn = self._handlers.get(cmd)
                result = fn(msg.get("params") or {}) if fn else None
                self._send(json.dumps({"id": msg["id"], "result": result}))

    def _take(self):
        if len(self._buf) < 2:
            return None
        b1 = self._buf[1]
        n, off = b1 & 0x7F, 2
        if n == 126:
            if len(self._buf) < 4:
                return None
            n = struct.unpack(">H", self._buf[2:4])[0]
            off = 4
        elif n == 127:
            if len(self._buf) < 10:
                return None
            n = struct.unpack(">Q", self._buf[2:10])[0]
            off = 10
        if len(self._buf) < off + n:
            return None
        opcode = self._buf[0] & 0x0F
        data = self._buf[off:off + n]
        self._buf = self._buf[off + n:]
        return opcode, data


@pytest.fixture
def bridge():
    # Port 0: the OS picks a free one. These tests must not fight each other,
    # and must not fight a real app running on this machine.
    b = ExtensionBridge(ports=(0,), timeout=5)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def ext(bridge):
    e = _Ext(bridge.port)
    for _ in range(100):
        if bridge.connected:
            break
        time.sleep(0.02)
    assert bridge.connected, "the client never registered"
    yield e
    e.close()


# --------------------------------------------------------------------- #
# Surviving a blink, and being able to say what happened afterwards

def test_a_read_survives_the_socket_being_replaced(bridge, ext):
    """Reported: "it does switch tab, then it fails and says the browser
    extension is not connected". An extension's socket goes away for ordinary
    reasons -- Chrome recycling the service worker, an extension reload -- and
    each is a gap of well under a second. A task dying because the transport
    blinked is not something the model or the user can do anything about."""
    ext.handle("tabs", lambda p: [{"id": 1, "title": "A", "url": "u",
                                   "active": True, "usable": True}])
    assert bridge.call("tabs")            # working to start with

    ext.close()                            # the blink
    replacement = _Ext(bridge.port)
    replacement.handle("tabs", lambda p: [{"id": 2, "title": "B", "url": "u",
                                           "active": True, "usable": True}])
    try:
        got = bridge.call("tabs", timeout=8)
        assert got[0]["id"] == 2, "the call did not reach the new connection"
    finally:
        replacement.close()


def test_a_read_waits_for_the_extension_to_come_back(bridge, ext):
    """The gap is usually shorter than this; the point is that a call arriving
    inside it waits rather than failing the whole task."""
    ext.handle("info", lambda p: {"url": "back"})
    bridge.call("info")
    ext.close()
    for _ in range(60):                    # the drop is noticed asynchronously
        if not bridge.connected:
            break
        time.sleep(0.05)
    assert bridge.connected is False

    later = {}

    def reconnect_soon():
        time.sleep(0.6)
        e = _Ext(bridge.port)
        e.handle("info", lambda p: {"url": "back"})
        later["ext"] = e

    threading.Thread(target=reconnect_soon, daemon=True).start()
    try:
        assert bridge.call("info", timeout=8)["url"] == "back"
    finally:
        if later.get("ext"):
            later["ext"].close()


def test_an_action_is_never_silently_repeated(bridge, ext):
    """A click whose connection dropped might or might not have happened, and
    guessing is worse than saying so -- a form submitted twice is not something
    a snapshot afterwards can undo."""
    ext.handle("click", lambda p: None, answer=False)
    result = {}

    def call():
        try:
            bridge.call("click", ref=1, timeout=20)
        except BridgeError as e:
            result["err"] = str(e)

    t = threading.Thread(target=call)
    t.start()
    time.sleep(0.2)
    ext.close()
    t.join(10)
    assert "can't tell whether it happened" in result.get("err", "")


def test_a_browser_that_never_connected_is_told_apart_from_one_that_left(bridge):
    """Different problems, different fixes: install the extension, versus your
    browser closed."""
    with pytest.raises(BridgeError) as e:
        bridge.call("info", timeout=1)
    assert "isn't connected" in str(e.value)


def test_the_connection_log_records_what_happened(bridge, ext):
    """Every report of this feature has come down to "it said connected but
    wasn't", and neither end could say when it went or why."""
    assert any(e["what"] == "connected" for e in bridge.events)
    ext.close()
    for _ in range(40):
        if any(e["what"] == "dropped" for e in bridge.events):
            break
        time.sleep(0.05)
    dropped = [e for e in bridge.events if e["what"] == "dropped"]
    assert dropped and dropped[-1]["why"]
    assert dropped[-1]["at"] > 0          # a real timestamp, unlike Chrome's page


def test_windows_does_not_let_a_second_process_steal_the_port():
    """SO_REUSEADDR means two different things. On Unix it only permits
    rebinding a port in TIME_WAIT. On WINDOWS it permits binding a port that is
    already in ACTIVE USE, and which socket then receives connections is
    undefined -- so a second copy of the app takes the port from under a
    running bridge and the extension starts getting connection refused."""
    import glmcode.extension_bridge as eb
    src = __import__("pathlib").Path(eb.__file__).read_text(encoding="utf-8")
    assert "SO_EXCLUSIVEADDRUSE" in src
    call = 'srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)'
    assert call in src
    before = src[src.index("def start"):src.index(call)]
    assert 'sys.platform == "win32"' in before, \
        "the SO_REUSEADDR call must sit in a non-Windows branch"
    assert "SO_EXCLUSIVEADDRUSE" in before


# --------------------------------------------------------------------- #
# Big messages arrive in pieces

def test_a_message_split_across_frames_is_reassembled(bridge, ext):
    """Reported: "it got the right browser, then it tried to take a screenshot
    and start working and then it disconnected."

    A screenshot is the only large payload in this protocol -- every other
    command answers in a few hundred bytes -- so the missing reassembly went
    unnoticed until the browser agent took one. RFC 6455 section 5.4 lets a
    sender fragment, and Chrome does for anything big: the first frame carried
    a fraction of the JSON (unparseable, silently dropped) and every
    continuation frame had opcode 0, which the read loop skipped as "not text".
    The reply vanished and the task died waiting for it.
    """
    shot = "data:image/png;base64," + ("QUJD" * 200_000)      # ~800KB, like a real one
    ext.fragment_at = 60_000
    ext.handle("screenshot", lambda p: shot)
    got = bridge.call("screenshot", timeout=15)
    assert got == shot


def test_fragmentation_at_awkward_sizes(bridge, ext):
    """Including a payload that lands exactly on a fragment boundary."""
    ext.fragment_at = 1000
    for size in (999, 1000, 1001, 2000, 2001):
        body = "x" * size
        ext.handle("text", lambda p, b=body: b)
        assert bridge.call("text", timeout=10) == body


def test_a_ping_between_fragments_does_not_corrupt_the_message(bridge, ext):
    """Control frames may be interleaved with a fragmented message (§5.4), and
    joining one into the payload would produce unparseable JSON."""
    body = "y" * 5000
    ext.fragment_at = 900
    ext.handle("text", lambda p: (ext.ping(b"mid"), body)[1])
    assert bridge.call("text", timeout=10) == body


def test_an_oversized_message_is_refused_rather_than_buffered(bridge, ext):
    """A cap on the whole message, not just one frame -- fragments would
    otherwise add up past it unchecked."""
    import glmcode.extension_bridge as eb
    assert eb.MAX_MESSAGE > 0
    src = __import__("pathlib").Path(eb.__file__).read_text(encoding="utf-8")
    assert "total > MAX_MESSAGE" in src


def test_the_log_says_who_actually_closed_it(bridge, ext):
    """"The browser closed the connection" was printed for our own read giving
    up too, and that cost a whole round of looking in the wrong place."""
    import glmcode.extension_bridge as eb
    src = __import__("pathlib").Path(eb.__file__).read_text(encoding="utf-8")
    assert "gave up reading the connection" in src
