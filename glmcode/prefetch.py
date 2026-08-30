"""The free work, done while nobody is waiting.

Between turns — while the diff is being read, while the next message is being
typed — the machine is idle and the day's request allowance is unspent. The
next turn then opens by paying for things that could have been ready: the
retrieval index gets rebuilt over whatever changed, the git history gets read
again, the same three or four seconds every time.

On a tier metered in requests per day, a saved round trip is worth more than a
smarter model. This spends none.

**It makes no model request. Ever.** That is the whole basis on which it is
allowed to run unasked, and it is why there is nothing to configure about how
much it may spend: the answer is nothing. Everything here is a local cache
being warmed — `codebase_memory`'s index over the files that changed, and
`riskmap`'s parse of the git log. Both are module-level caches keyed on
content, so warming them is invisible except in being faster.

Three rules, and the first two are what make it safe rather than merely
useful:

  - **It never touches the agent.** Not `messages`, not the turn lock, not any
    session state. It calls two pure-ish functions that populate caches keyed
    on file contents and on the HEAD sha. A background thread that wrote into
    agent state would produce exactly the `tool_call` with no matching reply
    that this codebase already has scar tissue about.

  - **It is cancelled by the next turn**, and checks the flag between steps.
    Work for a task that has been abandoned is pure heat, and a prefetch still
    running when the user asks something is competing with the thing they are
    waiting for.

  - **A failure is silent and total.** Nothing here is load-bearing: every
    single thing it does will be done again, correctly and synchronously, by
    whoever actually needs it. A warm cache that failed to warm is a slower
    turn, which is exactly what happens today.
"""

from __future__ import annotations

import threading
import time

# Between steps, so a cancel lands promptly without the thread spinning.
_STEP_PAUSE = 0.05


class Prefetch:
    """One background warm-up, cancellable, at most one at a time."""

    def __init__(self):
        self._cancel = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.last_steps = []          # what the last run actually did, for tests

    # -- lifecycle ---------------------------------------------------- #

    def start(self, workdir, changed_paths=None) -> bool:
        """Begin warming. Returns False if one is already running.

        Not queued: a second prefetch behind a first is warming caches for a
        state that has already moved on.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._cancel = threading.Event()
            self.last_steps = []
            self._thread = threading.Thread(
                target=self._run, args=(workdir, list(changed_paths or [])),
                name="mnm-prefetch", daemon=True)
            self._thread.start()
            return True

    def cancel(self, wait: float = 0.0) -> None:
        """Stop as soon as the current step finishes.

        `wait` is only for tests: a caller on the turn path must not block on
        this, since the entire point is that the user's next turn does not
        wait for work nobody asked for.
        """
        self._cancel.set()
        thread = self._thread
        if wait and thread is not None:
            thread.join(timeout=wait)

    def join(self, timeout: float = 10.0) -> bool:
        """Wait for the current warm-up to FINISH, without cancelling it.

        Distinct from `cancel(wait=...)`, which sets the flag first and so
        stops the work rather than waiting for it -- the tests reached for the
        latter to mean this and measured an aborted run. Nothing on the turn
        path calls this; it exists so a caller that genuinely wants the result
        (a test, a diagnostic) can have one.
        """
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the work ------------------------------------------------------ #

    def _stopped(self) -> bool:
        return self._cancel.is_set()

    def _run(self, workdir, changed_paths) -> None:
        for name, step in (("memory", self._warm_memory),
                           ("history", self._warm_history)):
            if self._stopped():
                return
            try:
                if step(workdir, changed_paths):
                    self.last_steps.append(name)
            except Exception:
                # Silent and total. Nothing here is load-bearing: whoever
                # actually needs this will do it again, synchronously and
                # correctly. A failed warm-up is a slower turn, which is what
                # happens today anyway.
                pass
            time.sleep(_STEP_PAUSE)

    def _warm_memory(self, workdir, changed_paths) -> bool:
        """Re-index the files this turn changed.

        `refresh()` is incremental and keyed on content, so this is the same
        work the next `search_code` would do -- moved to a moment when nobody
        is waiting for it. Skipped entirely when the turn changed nothing:
        there is nothing to re-read, and walking the tree to discover that is
        the cost this is supposed to avoid.
        """
        if not changed_paths:
            return False
        from pathlib import Path

        from . import codebase_memory
        index = codebase_memory.CodebaseIndex(Path(workdir))
        index.refresh()
        return True

    def _warm_history(self, workdir, changed_paths) -> bool:
        """Parse the git log into riskmap's cache.

        Keyed on the HEAD sha, so a commit invalidates it by itself and this
        can never serve a stale answer -- it can only make the first `risk`
        call, or the forgotten-sibling check at the end of the next turn,
        return without shelling out.
        """
        from . import riskmap
        return bool(riskmap.history(workdir))
