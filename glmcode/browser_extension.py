"""The app's single ExtensionBridge, and the setting that turns it on.

One bridge per process rather than one per chat: it is a listening socket and
the browser holds ONE connection into it, so a second server would just fail to
bind. Every chat that wants the user's own browser shares this one, the same
way they would share the browser itself.

Started lazily. Someone who never turns the setting on never has a port open.
"""

from __future__ import annotations

import threading

from .extension_bridge import BridgeError, ExtensionBridge

_lock = threading.Lock()
_bridge: ExtensionBridge | None = None
_listeners: list = []


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "browser_use_mine", False))


def bridge(start: bool = True) -> ExtensionBridge | None:
    """The process-wide bridge, started on first use. None if it can't bind."""
    global _bridge
    with _lock:
        if _bridge is not None:
            return _bridge
        if not start:
            return None
        b = ExtensionBridge()
        try:
            b.start()
        except BridgeError:
            return None
        b.on_change = _fanout
        _bridge = b
        return _bridge


def bridge_if_connected(cfg):
    """The bridge, but only when the setting is on AND a browser is on the end
    of it. Returning it while nothing is connected would make every browser
    action fail with 'the extension isn't connected' instead of falling through
    to a browser the app can actually launch."""
    if not enabled(cfg):
        return None
    b = bridge()
    return b if (b is not None and b.connected) else None


def status(cfg) -> dict:
    """What Settings shows: is it on, is the port up, is a browser on it."""
    b = bridge(start=enabled(cfg))
    return {
        "enabled": enabled(cfg),
        "port": b.port if b else None,
        "connected": bool(b and b.connected),
        "browser": (b.hello.get("agent") if b else "") or "",
        "version": (b.hello.get("version") if b else "") or "",
    }


def on_change(fn) -> None:
    """Register a callback for connect/disconnect, so the UI can say so
    without polling."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def _fanout(connected: bool) -> None:
    for fn in list(_listeners):
        try:
            fn(connected)
        except Exception:
            pass


def reset_for_tests() -> None:
    global _bridge
    with _lock:
        if _bridge is not None:
            _bridge.stop()
        _bridge = None
        _listeners.clear()
