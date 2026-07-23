"""Local browser screenshots via Playwright (no API key, runs on this machine).

Heavy dependency (the playwright package plus a downloaded Chromium build,
~150-300MB total) is never imported at module load time -- only inside
functions, and only once actually needed. On first use, the package is
installed via pip and `playwright install chromium` downloads the matching
browser build; everything after that runs fully offline (aside from
whatever the page itself loads).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

REQUIRED_PACKAGES = ["playwright"]

StatusFn = Optional[Callable[[str], None]]

# Screenshotting isn't meant for concurrent callers, and serializing avoids
# several sub-agents fighting over the same first-run install/download.
_lock = threading.Lock()


def _launch_kwargs() -> dict:
    """Point Playwright at a specific Chromium binary when MNM_CHROMIUM_PATH is
    set -- for machines with a pre-installed browser (or a pinned build) where
    the bundled download isn't available. Empty otherwise (Playwright's own)."""
    exe = os.environ.get("MNM_CHROMIUM_PATH", "").strip()
    return {"executable_path": exe} if exe else {}


def packages_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


def _chromium_installed() -> bool:
    """Cheap check: can we actually launch, not just import the package.
    A pip install without a matching `playwright install chromium` leaves
    the package importable but browser-less."""
    if not packages_installed():
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(**_launch_kwargs())
            browser.close()
        return True
    except Exception:
        return False


def ready() -> bool:
    return _chromium_installed()


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status("Installing local browser-preview dependencies (first time only, "
              "~150-300MB download)...")
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", *REQUIRED_PACKAGES]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **NO_WINDOW_KWARGS)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Installing playwright timed out after 5 minutes.")
    except OSError as e:
        raise RuntimeError(f"Could not start pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to install playwright:\n{tail}")

    if status:
        status("Downloading Chromium for browser preview (first time only)...")
    cmd2 = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        proc2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=600, **NO_WINDOW_KWARGS)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Downloading Chromium timed out after 10 minutes.")
    except OSError as e:
        raise RuntimeError(f"Could not run 'playwright install': {e}")
    if proc2.returncode != 0:
        tail = (proc2.stderr or proc2.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to download Chromium:\n{tail}")


def preview_page(url: str, out_path: Path, wait_seconds: float = 2.0,
                  full_page: bool = True, status: StatusFn = None) -> Path:
    """Load a URL in headless Chromium and save a screenshot as a PNG."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url must not be empty")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    wait_seconds = max(0.0, min(float(wait_seconds or 2.0), 15.0))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        if not ready():
            _install_packages(status)

        from playwright.sync_api import sync_playwright
        if status:
            status(f"Loading {url}...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**_launch_kwargs())
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 800})
                    try:
                        page.goto(url, wait_until="load", timeout=20_000)
                    except Exception as e:
                        raise RuntimeError(
                            f"Could not load {url}: {e}. If this is a local dev server, "
                            f"make sure it's actually running (see run_background)."
                        )
                    if wait_seconds:
                        page.wait_for_timeout(wait_seconds * 1000)
                    page.screenshot(path=str(out_path), full_page=full_page)
                finally:
                    browser.close()
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Browser preview failed: {e}")
    return out_path


def capture_page(url: str, out_path: Path, wait_seconds: float = 2.5,
                 status: StatusFn = None) -> dict:
    """Load a URL in headless Chromium like preview_page, but ALSO capture what
    happens at runtime: console errors/warnings, uncaught exceptions, and failed
    network requests. Returns {screenshot, console, page_errors, failed_requests,
    load_error} so the agent can fix what actually breaks when the app runs, not
    just what compiles."""
    url = (url or "").strip()
    if not url:
        raise ValueError("url must not be empty")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "http://" + url
    wait_seconds = max(0.0, min(float(wait_seconds or 2.5), 20.0))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = {"screenshot": "", "console": [], "page_errors": [],
              "failed_requests": [], "load_error": ""}

    with _lock:
        if not ready():
            _install_packages(status)
        from playwright.sync_api import sync_playwright
        if status:
            status(f"Running {url} and watching for errors...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**_launch_kwargs())
                try:
                    page = browser.new_page(viewport={"width": 1280, "height": 800})

                    def on_console(msg):
                        if msg.type in ("error", "warning"):
                            result["console"].append(f"[{msg.type}] {msg.text}"[:500])

                    page.on("console", on_console)
                    page.on("pageerror",
                            lambda exc: result["page_errors"].append(str(exc)[:500]))
                    page.on("requestfailed", lambda req: result["failed_requests"].append(
                        f"{req.method} {req.url} — {(req.failure or '')}"[:300]))
                    try:
                        page.goto(url, wait_until="load", timeout=20_000)
                    except Exception as e:
                        result["load_error"] = (
                            f"Could not load {url}: {e}. If this is a local dev server, make "
                            f"sure it's actually running (run_background).")
                    if wait_seconds:
                        page.wait_for_timeout(wait_seconds * 1000)
                    try:
                        page.screenshot(path=str(out_path), full_page=True)
                        result["screenshot"] = str(out_path)
                    except Exception:
                        pass
                finally:
                    browser.close()
        except Exception as e:
            if not result["load_error"]:
                result["load_error"] = f"Browser run failed: {e}"
    # De-duplicate while preserving order (a noisy app repeats the same error).
    for key in ("console", "page_errors", "failed_requests"):
        seen, uniq = set(), []
        for item in result[key]:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        result[key] = uniq[:25]
    return result
