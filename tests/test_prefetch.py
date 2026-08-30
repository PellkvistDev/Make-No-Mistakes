"""The free work done between turns.

The properties that matter are the safety ones. A background thread that runs
unasked has to be provably unable to (a) spend a request, (b) touch the
agent's state, or (c) outlive the moment it was useful — and the tests are
mostly about those three rather than about it being fast.
"""

import subprocess
import threading
from types import SimpleNamespace

import pytest

from glmcode import prefetch as P
from glmcode.agent import Agent


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "Tester"]):
        subprocess.run(["git"] + args, cwd=str(r), check=True, capture_output=True)
    (r / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(r), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=str(r), check=True,
                   capture_output=True)
    return r


# --------------------------------------------------------------------- #
# It does the work

def test_it_warms_the_history_cache(repo):
    from glmcode import riskmap
    riskmap._cache.clear()
    p = P.Prefetch()
    assert p.start(repo) is True
    assert p.join(10) is True
    assert "history" in p.last_steps
    assert riskmap._cache, "the git log should now be parsed and cached"


def test_a_turn_that_changed_nothing_does_not_reindex(repo):
    """Walking the tree to discover there is nothing to re-read is exactly the
    cost this exists to avoid."""
    p = P.Prefetch()
    p.start(repo, changed_paths=[])
    p.join(10)
    assert "memory" not in p.last_steps


def test_changed_files_do_trigger_the_index(repo, monkeypatch):
    called = []

    class FakeIndex:
        def __init__(self, root):
            called.append(root)

        def refresh(self):
            return 1

    from glmcode import codebase_memory
    monkeypatch.setattr(codebase_memory, "CodebaseIndex", FakeIndex)
    p = P.Prefetch()
    p.start(repo, changed_paths=["a.py"])
    p.join(10)
    assert called, "the index should have been refreshed"
    assert "memory" in p.last_steps


# --------------------------------------------------------------------- #
# It must never outlive its usefulness

def test_only_one_runs_at_a_time(repo):
    """A second prefetch behind a first is warming caches for a state that has
    already moved on."""
    started = threading.Event()
    release = threading.Event()

    p = P.Prefetch()

    def slow(workdir, changed):
        started.set()
        release.wait(timeout=5)
        return True

    p._warm_history = slow
    assert p.start(repo) is True
    started.wait(timeout=5)
    assert p.start(repo) is False, "a second start must be refused, not queued"
    release.set()
    p.cancel(wait=5)


def test_cancelling_stops_it_between_steps(repo):
    p = P.Prefetch()
    ran = []

    def first(workdir, changed):
        p.cancel()          # cancel from inside the first step
        ran.append("first")
        return True

    def second(workdir, changed):
        ran.append("second")
        return True

    p._warm_memory = first
    p._warm_history = second
    p.start(repo, changed_paths=["a.py"])
    if p._thread:
        p._thread.join(timeout=5)
    assert ran == ["first"], "the step after a cancel must not run"


def test_cancel_does_not_block_the_caller(repo):
    """The user's next turn must never queue behind work nobody asked for."""
    release = threading.Event()
    p = P.Prefetch()
    p._warm_history = lambda w, c: release.wait(timeout=5)
    p.start(repo)
    import time
    began = time.time()
    p.cancel()                       # no wait argument
    assert time.time() - began < 0.5
    release.set()


# --------------------------------------------------------------------- #
# A failure must be invisible

def test_a_step_that_raises_does_not_stop_the_others(repo):
    p = P.Prefetch()

    def boom(workdir, changed):
        raise RuntimeError("nope")

    p._warm_memory = boom
    p.start(repo, changed_paths=["a.py"])
    p.join(10)
    assert "history" in p.last_steps
    assert "memory" not in p.last_steps


def test_a_directory_that_is_not_a_repo_is_survived(tmp_path):
    p = P.Prefetch()
    p.start(tmp_path)
    p.join(10)
    assert p.last_steps == [] or "history" not in p.last_steps


# --------------------------------------------------------------------- #
# The agent side

def _agent(**kw):
    base = dict(conversational=False, allow_subagents=True,
                cfg=SimpleNamespace(prefetch_between_turns=True),
                workdir="/proj", _prefetch=None, _turn_wrote_paths=set())
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_sub_agent_never_prefetches():
    """A worker finishing is not a moment when nobody is waiting — the
    coordinator is."""
    a = _agent(allow_subagents=False)
    Agent._start_prefetch(a)
    assert a._prefetch is None


def test_the_voice_delegator_never_prefetches():
    a = _agent(conversational=True)
    Agent._start_prefetch(a)
    assert a._prefetch is None


def test_turning_it_off_means_nothing_starts():
    a = _agent(cfg=SimpleNamespace(prefetch_between_turns=False))
    Agent._start_prefetch(a)
    assert a._prefetch is None


def test_cancelling_before_anything_started_is_harmless():
    a = _agent()
    Agent._cancel_prefetch(a)     # must not raise
    a2 = SimpleNamespace()        # not even the attribute
    Agent._cancel_prefetch(a2)


def test_it_never_reaches_for_a_model_client():
    """The basis on which this is allowed to run unasked. If it ever needed a
    client, it would need a budget, and it has neither."""
    import inspect
    src = inspect.getsource(P)
    for forbidden in ("ZaiClient", "chat(", "_call_model", "completions"):
        assert forbidden not in src, f"prefetch must not reference {forbidden}"
