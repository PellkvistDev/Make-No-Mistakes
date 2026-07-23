"""Tiny QR-code helper for the desktop app.

Wraps `segno` (pure-Python, zero-dependency) to turn a URL into an inline SVG we
can hand straight to the webview. Kept as a separate, import-light module so it's
unit-testable without the GUI, and so a missing `segno` degrades to a clear
message instead of crashing the app.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import segno  # noqa: F401
        return True
    except Exception:
        return False


def qr_svg(text: str, *, scale: int = 6, border: int = 3,
           dark: str = "#0b0d10", light: str = "#ffffff") -> str:
    """Return an inline `<svg>…</svg>` string encoding *text*.

    Uses error-correction level M (good balance for a URL). Raises RuntimeError
    with a friendly message if segno isn't installed, so the caller can surface
    it in the UI rather than blowing up.
    """
    if not text or not str(text).strip():
        raise ValueError("nothing to encode")
    try:
        import segno
    except Exception as e:  # pragma: no cover - exercised only without segno
        raise RuntimeError("segno isn't installed (pip install segno)") from e
    qr = segno.make(str(text).strip(), error="m")
    return qr.svg_inline(scale=scale, border=border, dark=dark, light=light)
