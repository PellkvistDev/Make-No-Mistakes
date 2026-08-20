"""Which Chromium browsers this machine has, and opening their extensions page.

The extension has to be loaded by hand -- Chrome has no API for installing an
unpacked extension, and the Web Store is not a route this project can take. So
the only thing left is to remove every step around it that CAN be removed.

Naming the browsers matters more than it looks. "Open chrome://extensions"
assumes one browser and assumes it is Chrome; someone who lives in Edge or
Brave reads that and either gives up or installs it in the wrong browser and
wonders why nothing connects.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .tools import NO_WINDOW_KWARGS

# name -> the places it lives, most likely first. Chromium-based only: the
# extension is MV3 and there is nothing here for Firefox or Safari.
_MAC = {
    "Google Chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Microsoft Edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "Brave": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
    "Arc": ["/Applications/Arc.app/Contents/MacOS/Arc"],
    "Vivaldi": ["/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"],
    "Chromium": ["/Applications/Chromium.app/Contents/MacOS/Chromium"],
}
_WIN = {
    "Google Chrome": [r"Google\Chrome\Application\chrome.exe"],
    "Microsoft Edge": [r"Microsoft\Edge\Application\msedge.exe"],
    "Brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "Vivaldi": [r"Vivaldi\Application\vivaldi.exe"],
}
_LINUX = {
    "Google Chrome": ["google-chrome", "google-chrome-stable"],
    "Microsoft Edge": ["microsoft-edge", "microsoft-edge-stable"],
    "Brave": ["brave-browser", "brave"],
    "Vivaldi": ["vivaldi", "vivaldi-stable"],
    "Chromium": ["chromium", "chromium-browser"],
}


def _windows_roots() -> list[Path]:
    roots = []
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        val = os.environ.get(var, "").strip()
        if val:
            roots.append(Path(val))
    return roots


def find() -> list[dict]:
    """Every Chromium browser on this machine, as {name, path}.

    Order is stable and by popularity rather than by what the filesystem
    happens to return, because the list is rendered as buttons and the first
    one is what most people will click.
    """
    found: list[dict] = []
    if sys.platform == "darwin":
        table = _MAC
        for name, paths in table.items():
            for p in paths:
                if Path(p).exists():
                    found.append({"name": name, "path": p})
                    break
    elif sys.platform == "win32":
        roots = _windows_roots()
        for name, rels in _WIN.items():
            for rel in rels:
                hit = next((r / rel for r in roots if (r / rel).is_file()), None)
                if hit:
                    found.append({"name": name, "path": str(hit)})
                    break
    else:
        for name, commands in _LINUX.items():
            for cmd in commands:
                path = shutil.which(cmd)
                if path:
                    found.append({"name": name, "path": path})
                    break
    return found


def open_extensions_page(path: str) -> tuple[bool, str]:
    """Open chrome://extensions in that browser. Returns (ok, error).

    Passing a chrome:// URL on the command line is the one way to reach that
    page from outside the browser -- it cannot be linked to, and a running
    browser handles the argument by opening a tab rather than starting a
    second copy, which is exactly what is wanted here.
    """
    exe = Path(path)
    if not exe.exists():
        return False, f"That browser isn't where it was: {path}"
    try:
        subprocess.Popen([str(exe), "chrome://extensions"], **NO_WINDOW_KWARGS)
    except OSError as e:
        return False, f"Couldn't start {exe.name}: {e}"
    return True, ""
