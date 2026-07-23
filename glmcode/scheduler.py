"""Scheduled & watched tasks: saved prompts that run themselves later.

Explicit and opt-in by design (no ambient daemon): the user creates tasks; a
lightweight poller in the GUI fires the ones that are due. Three trigger kinds:

  interval  every N minutes
  daily     once a day at a local HH:MM
  watch     when a folder's contents change (checked on each poll tick)

This module is pure and side-effect-free -- it decides *what* is due; the GUI
owns actually running a task and recording last_run. That split keeps the
timing logic fully testable without threads, clocks, or the agent.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from pathlib import Path

SCHEDULE_KINDS = ("interval", "daily", "watch")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
MIN_INTERVAL_MIN = 5
MAX_TASKS = 50


def new_task_id() -> str:
    return "task_" + uuid.uuid4().hex[:10]


def normalize_task(raw: dict) -> dict | None:
    """Validate + canonicalise a task from the UI. Returns None if invalid."""
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name", "")).strip()[:80]
    prompt = str(raw.get("prompt", "")).strip()
    cwd = str(raw.get("cwd", "")).strip()
    if not prompt or not cwd:
        return None
    sched = raw.get("schedule") or {}
    kind = str(sched.get("kind", "")).strip().lower()
    if kind not in SCHEDULE_KINDS:
        return None
    out_sched: dict
    if kind == "interval":
        try:
            minutes = int(sched.get("minutes", 0))
        except (TypeError, ValueError):
            return None
        if minutes < MIN_INTERVAL_MIN:
            return None
        out_sched = {"kind": "interval", "minutes": minutes}
    elif kind == "daily":
        at = str(sched.get("at", "")).strip()
        if not _HHMM.match(at):
            return None
        out_sched = {"kind": "daily", "at": at}
    else:  # watch
        watch_path = str(sched.get("path", "")).strip() or cwd
        out_sched = {"kind": "watch", "path": watch_path}
    return {
        "id": str(raw.get("id") or new_task_id()),
        "name": name or (prompt[:40] + ("…" if len(prompt) > 40 else "")),
        "prompt": prompt,
        "cwd": cwd,
        "schedule": out_sched,
        "enabled": bool(raw.get("enabled", True)),
        "last_run": float(raw.get("last_run", 0) or 0),
        # for watch tasks: signature of the folder at the last check/run
        "last_sig": str(raw.get("last_sig", "")),
    }


def _interval_due(task: dict, now: float) -> bool:
    minutes = task["schedule"].get("minutes", 0)
    return (now - task.get("last_run", 0)) >= minutes * 60


def _daily_due(task: dict, now: float) -> bool:
    m = _HHMM.match(task["schedule"].get("at", ""))
    if not m:
        return False
    hh, mm = int(m.group(1)), int(m.group(2))
    now_dt = datetime.fromtimestamp(now)
    target = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now_dt < target:
        return False                       # not yet today
    return task.get("last_run", 0) < target.timestamp()   # not already run today


def folder_signature(path: str, max_entries: int = 5000) -> str:
    """A cheap change-signature for a folder: newest mtime + file count + total
    size over its (non-hidden) files. Changes when files are added, removed, or
    edited -- enough for a watch trigger without an OS file watcher."""
    import os
    root = Path(path)
    if not root.is_dir():
        return ""
    newest = 0.0
    count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                st = (Path(dirpath) / name).stat()
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            total += st.st_size
            count += 1
            if count >= max_entries:
                break
    return f"{int(newest)}:{count}:{total}"


def _watch_due(task: dict, sig: str) -> bool:
    # Fire when the folder changed since the last check, but never on the very
    # first observation (we just record the baseline then).
    if not sig:
        return False
    last = task.get("last_sig", "")
    return bool(last) and sig != last


def is_due(task: dict, now: float | None = None, sig: str | None = None) -> bool:
    """Should this task fire now? `sig` is the folder signature for watch tasks
    (the caller computes it, so this stays pure/testable)."""
    if not task.get("enabled", True):
        return False
    now = time.time() if now is None else now
    kind = task["schedule"]["kind"]
    if kind == "interval":
        return _interval_due(task, now)
    if kind == "daily":
        return _daily_due(task, now)
    if kind == "watch":
        return _watch_due(task, sig if sig is not None else folder_signature(task["schedule"]["path"]))
    return False


def due_tasks(tasks: list, now: float | None = None, sig_func=folder_signature) -> list:
    """The subset of `tasks` that should fire now. For watch tasks, sig_func is
    called with the watched path (injectable for tests)."""
    now = time.time() if now is None else now
    out = []
    for t in tasks:
        try:
            sig = sig_func(t["schedule"]["path"]) if t["schedule"]["kind"] == "watch" else None
            if is_due(t, now, sig):
                out.append(t)
        except Exception:
            continue
    return out


def describe(task: dict) -> str:
    """A short human label for the UI, e.g. 'every 30 min' / 'daily at 09:00'."""
    s = task["schedule"]
    if s["kind"] == "interval":
        n = s["minutes"]
        return f"every {n} min" if n < 60 or n % 60 else f"every {n // 60} h"
    if s["kind"] == "daily":
        return f"daily at {s['at']}"
    return "on change"
