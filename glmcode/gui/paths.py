"""Where the app's own files are.

A leaf, so that anything needing one of these can have it without importing
app.py. `__file__` is in this same package directory, so every path resolves
exactly as it did when these lived in app.py.
"""

from __future__ import annotations

from pathlib import Path


WEB_DIR = Path(__file__).parent / "web"
DEFAULT_BG = WEB_DIR / "bg-default.jpg"
# Always-available scratch folder for quick, throwaway projects -- a sibling
# of this app's own install directory (e.g. .../Theo/Make No Mistakes ->
# .../Theo/whiteboard), created on first use rather than at import time.
WHITEBOARD_DIR = Path(__file__).resolve().parents[3] / "whiteboard"

# The unpacked browser extension the user loads into their own browser. A
# sibling of the package rather than inside it: "Load unpacked" wants a plain
# folder, and one buried in site-packages is a folder nobody can find.
EXTENSION_DIR = Path(__file__).resolve().parents[2] / "extension"
