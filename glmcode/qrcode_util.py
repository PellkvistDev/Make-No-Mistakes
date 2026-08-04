"""Tiny QR-code helper for the desktop app.

Wraps `segno` (pure-Python, zero-dependency) to turn a URL into an inline SVG we
can hand straight to the webview. Kept as a separate, import-light module so it's
unit-testable without the GUI, and so a missing `segno` degrades to a clear
message instead of crashing the app.
"""
from __future__ import annotations

import re


def available() -> bool:
    try:
        import segno  # noqa: F401
        return True
    except Exception:
        return False


def qr_svg(text: str, *, scale: int = 6, border: int = 3,
           dark: str = "#0b0d10", light: str = "#ffffff", error: str = "m") -> str:
    """Return an inline `<svg>…</svg>` string encoding *text*.

    Error-correction defaults to M, a good balance for a short URL. A long
    payload (the pairing link carries an encrypted blob) should pass "l": the
    redundancy that helps a printed code survive a scuff is wasted on a clean
    screen, and dropping it keeps the modules big enough for a phone to read.

    Raises RuntimeError with a friendly message if segno isn't installed, so the
    caller can surface it in the UI rather than blowing up.
    """
    if not text or not str(text).strip():
        raise ValueError("nothing to encode")
    try:
        import segno
    except Exception as e:  # pragma: no cover - exercised only without segno
        raise RuntimeError("segno isn't installed (pip install segno)") from e
    qr = segno.make(str(text).strip(), error=error)
    svg = qr.svg_inline(scale=scale, border=border, dark=dark, light=light)
    return _make_scalable(svg)


def _make_scalable(svg: str) -> str:
    """Give the SVG a viewBox and drop its fixed width/height.

    segno emits `<svg width="405" height="405">` with no viewBox, so the drawing
    has a natural size and CSS can only resize the *element* -- the paths keep
    drawing at 405px and spill out of whatever box they're in. A bigger payload
    means a bigger natural size, so this gets worse exactly when the QR matters
    most. With a viewBox and no intrinsic size, CSS scales it properly.
    """
    m = re.match(r'<svg\s+width="(\d+(?:\.\d+)?)"\s+height="(\d+(?:\.\d+)?)"', svg)
    if not m:
        return svg   # segno changed its output; leave it rather than mangle it
    w, h = m.group(1), m.group(2)
    return svg.replace(m.group(0), f'<svg viewBox="0 0 {w} {h}"', 1)
