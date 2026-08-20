"""Updating the app in place: what `git pull` would do, with the sharp edges off.

This app is installed by cloning it, so an update is a pull and a restart. Both
halves have failure modes that are invisible from a button:

  - The checkout may have local edits, be on a branch that isn't `main`, be
    mid-rebase, or not be a git checkout at all (a copied folder, a zip).
  - A pull can conflict, and a half-merged working tree is a worse state than
    the one it started in.
  - Restarting has to hand over to a NEW process before this one exits, or the
    user is left with no app and no explanation.

So the button is two steps, not one. `check()` looks, without changing
anything, and says what would happen. `pull()` only runs when that came back
clean, and refuses rather than guessing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .gui.paths import APP_ROOT
from .tools import NO_WINDOW_KWARGS

# A pull is a network call; a stuck one should not freeze the settings panel.
FETCH_TIMEOUT = 25
PULL_TIMEOUT = 60


def _git(args: list[str], timeout: int = 15, cwd: Path | None = None) -> tuple[int, str]:
    """Run git with an argv list. Never raises; returns (code, output)."""
    try:
        r = subprocess.run(["git", "-C", str(cwd or APP_ROOT), *args],
                           capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace", **NO_WINDOW_KWARGS)
    except FileNotFoundError:
        return 127, "git is not installed, or not on PATH."
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0]} took longer than {timeout}s."
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def check() -> dict:
    """Is there an update, and can it be taken safely? Changes nothing.

    Every refusal names the actual state, because "couldn't update" on its own
    leaves someone with a button that does not work and no idea why.
    """
    code, _ = _git(["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        return {"ok": False,
                "reason": "This copy isn't a git checkout, so there's nothing "
                          "to pull. Updates only work when the app was cloned "
                          "with git.",
                "path": str(APP_ROOT)}

    code, branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip()
    if code != 0 or branch == "HEAD":
        return {"ok": False,
                "reason": "This checkout isn't on a branch (detached HEAD). "
                          "Check out a branch before updating.",
                "path": str(APP_ROOT)}

    code, dirty = _git(["status", "--porcelain"])
    if code == 0 and dirty.strip():
        n = len([ln for ln in dirty.splitlines() if ln.strip()])
        return {"ok": False, "branch": branch, "dirty": n,
                "reason": f"There {'is' if n == 1 else 'are'} {n} uncommitted "
                          f"change{'' if n == 1 else 's'} here. Updating could "
                          "overwrite them, so it's refused -- commit or discard "
                          "them first.",
                "path": str(APP_ROOT)}

    code, out = _git(["fetch", "--quiet"], timeout=FETCH_TIMEOUT)
    if code != 0:
        return {"ok": False, "branch": branch,
                "reason": f"Couldn't reach the remote: {out or 'no details'}",
                "path": str(APP_ROOT)}

    code, counts = _git(["rev-list", "--left-right", "--count",
                         f"{branch}...@{{upstream}}"])
    if code != 0:
        return {"ok": False, "branch": branch,
                "reason": f"'{branch}' isn't tracking a remote branch, so "
                          "there's nothing to pull from.",
                "path": str(APP_ROOT)}
    try:
        ahead, behind = (int(x) for x in counts.split())
    except ValueError:
        ahead, behind = 0, 0

    log = ""
    if behind:
        _, log = _git(["log", "--oneline", "--no-decorate", "-n", "10",
                       f"HEAD..@{{upstream}}"])
    return {"ok": True, "branch": branch, "ahead": ahead, "behind": behind,
            "changes": [ln for ln in log.splitlines() if ln.strip()],
            "path": str(APP_ROOT),
            "reason": "" if behind else "Already up to date."}


def pull() -> dict:
    """Take the update. Only ever called after a clean check().

    --ff-only on purpose: a merge commit created by a button, in someone's app
    directory, is not something they asked for -- and a CONFLICTED merge would
    leave the app in a state it cannot run from. Refusing is recoverable;
    half-merging is not.
    """
    state = check()
    if not state.get("ok"):
        return state
    if not state.get("behind"):
        return {"ok": True, "updated": False, "reason": "Already up to date."}

    code, out = _git(["pull", "--ff-only"], timeout=PULL_TIMEOUT)
    if code != 0:
        return {"ok": False, "branch": state["branch"], "path": str(APP_ROOT),
                "reason": f"The update wouldn't apply cleanly: {out or 'no details'}. "
                          "Nothing was changed."}
    return {"ok": True, "updated": True, "branch": state["branch"],
            "changes": state.get("changes", []),
            "count": state.get("behind", 0)}


def restart_command() -> list[str]:
    """How to start this app again. `python -m glmcode.gui`, with whatever
    interpreter is running now -- not a hard-coded 'python', which on Windows
    may be a different install or absent from PATH entirely."""
    return [sys.executable, "-m", "glmcode.gui"]


def spawn_restart() -> bool:
    """Start a fresh copy of the app, detached from this one.

    Detached matters: the new process must outlive this one, and on Windows a
    child of a dying parent in the same console group goes down with it. The
    caller closes the window immediately afterwards, so anything that ties the
    two together means the update looks like the app simply quitting.
    """
    kwargs = dict(cwd=str(APP_ROOT), **NO_WINDOW_KWARGS)
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(restart_command(), **kwargs)
        return True
    except OSError:
        return False
