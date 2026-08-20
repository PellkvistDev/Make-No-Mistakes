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
    """Whether the user's own browser may be used at all.

    On unless they turned it off. The extension being installed IS the opt-in:
    loading an unpacked extension into your own browser is a deliberate,
    several-step act, and requiring a switch afterwards meant people finished
    the hard part and then found nothing worked.
    """
    return str(getattr(cfg, "browser_own", "auto") or "auto") != "off"


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


def not_connected_hint() -> str:
    """What to say when 'use my own browser' is on but nothing is on the end.

    Names the state AND the next move, because the observable symptom is a
    second browser window opening with a blank tab, and nothing about that says
    "your extension isn't connected".
    """
    b = bridge(start=True)
    where = f"port {b.port}" if b and b.port else "a local port"
    return ("'Use my own browser' is on, but the extension isn't connected, so "
            f"I'm falling back to a separate browser window. The app is "
            f"listening on {where}. Check: is the browser you installed the "
            "extension in actually open? Is its toolbar button showing a pause "
            "mark (click it to resume)? Settings -> Browser says Connected the "
            "moment both ends are up.")


def status(cfg, listen: bool = False) -> dict:
    """What Settings shows: is it on, is the port up, is a browser on it.

    `listen` opens the port even when the setting is off, and the Settings
    panel passes it while it is on screen. Without that the install sheet said
    "Waiting for the extension..." forever for anyone who had not flipped the
    switch first -- there was nothing to wait on, because the port only opened
    when the feature was already enabled. Being able to verify the install
    BEFORE turning it on is the right order for a feature that hands over a
    logged-in browser.
    """
    b = bridge(start=listen or enabled(cfg))
    return {
        "enabled": enabled(cfg),
        "port": b.port if b else None,
        "connected": bool(b and b.connected),
        "browser": (b.hello.get("agent") if b else "") or "",
        "version": (b.hello.get("version") if b else "") or "",
        # What the connection actually did, most recent first. Every report of
        # this feature so far has come down to "it said connected but wasn't",
        # and neither end could say when it went or why. Chrome's extensions
        # page shows errors with no usable timestamp, so it cannot settle it
        # either. This can.
        "log": list(reversed(b.events)) if b else [],
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
