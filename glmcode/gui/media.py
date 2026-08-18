"""Turning a file on disk into something an <img> or <audio> tag can hold.

The page is loaded from a local directory and cannot read arbitrary paths, so
every image the app shows -- a screenshot a tool produced, an attachment, the
chosen background -- crosses the bridge as a data URI. A leaf module for the
same reason as speech.py: the event sink and the Api both need it.
"""

from __future__ import annotations

import base64
import io
import mimetypes
from pathlib import Path


def _data_uri(path: Path, max_bytes: int = 12_000_000) -> str:
    data = path.read_bytes()[:max_bytes]
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _thumb_uri(path: Path, size: int = 360) -> str:
    try:
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return _data_uri(path)
