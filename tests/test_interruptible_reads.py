"""A network read cannot hang the turn, and Stop reaches it.

Found by scanning rather than by being reported, which is the point of having
scanned. Two facts that only bite together:

  - `web_search`'s default backend is DuckDuckGo whenever no Tavily key is set
    -- the ordinary case on the free tier this app is built around -- and it
    constructed DDGS() with NO timeout at all.
  - Stop reaches a shell command (kill the process tree) and a sub-agent (its
    own cancel Event). A network read has neither, so it ignored Stop.

So a hung search was unbounded AND unstoppable: no way out but killing the app.

You cannot kill a thread in Python. What `interruptible` does is stop WAITING
for one -- the call is abandoned and finishes into nothing on its own timeout.
That is only defensible for a read, the same line the extension bridge draws
with SAFE_TO_REPEAT.
"""

import inspect
import threading
import time

import pytest

from glmcode import tools as T
from glmcode.errors import ToolError as ToolErrorBase


@pytest.fixture(autouse=True)
def _token():
    T.set_call_token("tok-test")
    T.clear_cancelled("tok-test")
    yield
    T.clear_cancelled("tok-test")
    T.set_call_token(None)


# ------------------------------------------------- the mechanism ---------

def test_a_call_that_returns_is_just_returned():
    assert T.interruptible(lambda: 41 + 1) == 42


def test_arguments_go_through():
    assert T.interruptible(lambda a, b=0: a + b, 40, b=2) == 42


def test_an_exception_comes_back_as_itself():
    def boom():
        raise ValueError("nope")
    with pytest.raises(ValueError, match="nope"):
        T.interruptible(boom)


def test_a_hung_call_gives_up_when_the_turn_is_stopped():
    """The whole point. Without this the turn waits on the far end."""
    started = threading.Event()

    def hang():
        started.set()
        time.sleep(30)
        return "never"

    def stop_soon():
        started.wait(5)
        time.sleep(0.05)
        T.mark_cancelled("tok-test")

    threading.Thread(target=stop_soon, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(ToolErrorBase):
        T.interruptible(hang)
    assert time.monotonic() - t0 < 5, "it waited out the hung call anyway"


def test_it_does_not_wait_for_the_abandoned_call():
    """Abandoned, not cancelled -- and the turn must not block on it winding
    down, or giving up would buy nothing.

    The cancel is raised from another thread rather than set up front, because
    interruptible clears a stale one on entry (see the test below): a pre-set
    flag proves nothing about a call that is already in flight, which is the
    only case that matters."""
    started = threading.Event()

    def hang():
        started.set()
        time.sleep(20)

    def stop_soon():
        started.wait(5)
        T.mark_cancelled("tok-test")

    threading.Thread(target=stop_soon, daemon=True).start()
    t0 = time.monotonic()
    with pytest.raises(ToolErrorBase):
        T.interruptible(hang)
    # The 20s call is still going; returning must not have waited for it.
    assert time.monotonic() - t0 < 3


def test_the_pool_is_not_joined_on_the_way_out():
    """Read from the source, because the timing test above would also pass if
    the thread happened to finish early."""
    assert "shutdown(wait=False)" in inspect.getsource(T.interruptible)


def test_a_stale_cancel_does_not_kill_the_next_read():
    """Tokens are per tool call, but a cancel left set would make the next
    read under a reused token fail instantly for no reason."""
    T.mark_cancelled("tok-test")
    assert T.interruptible(lambda: "fine") == "fine"


def test_no_token_means_no_machinery():
    """The CLI and the tests have nothing to stop; the call should not be
    pushed onto a thread for nothing."""
    T.set_call_token(None)
    assert T.interruptible(lambda: "direct") == "direct"


def test_the_cancelled_set_cannot_grow_without_bound():
    for i in range(600):
        T.mark_cancelled(f"t{i}")
    assert len(T._cancelled_tokens) <= 512


# ------------------------------------------------- the tools -------------

def test_the_default_search_backend_has_a_timeout():
    """It had none, and it is the default."""
    src = inspect.getsource(T._search_duckduckgo)
    assert "DDGS(timeout=" in src


def test_both_search_backends_are_bounded_by_the_same_number():
    assert "timeout=SEARCH_TIMEOUT" in inspect.getsource(T._search_tavily)
    assert "DDGS(timeout=SEARCH_TIMEOUT)" in inspect.getsource(T._search_duckduckgo)
    assert 0 < T.SEARCH_TIMEOUT <= 60


def test_web_search_goes_through_the_interruptible_path():
    src = inspect.getsource(T.web_search)
    assert src.count("interruptible(") >= 2, "one of the backends still blocks"


def test_fetch_url_does_too_and_keeps_its_own_errors():
    src = inspect.getsource(T.fetch_url)
    assert "interruptible(" in src
    # A stop must not be re-wrapped as "Fetch failed", which would read as the
    # site being broken rather than as the user having pressed Stop.
    assert "except ToolErrorBase:" in src


def test_a_write_tool_is_not_abandonable():
    """Only reads go through this. A command that might have half-run is not
    something to walk away from -- run_command is stopped by killing its
    process, which is a different and harder guarantee."""
    assert "interruptible(" not in inspect.getsource(T.run_command)


# ------------------------------------------------- and Stop reaches it ---

def test_cancelling_a_turn_marks_the_token():
    from glmcode.agent import Agent
    src = inspect.getsource(Agent.request_cancel)
    assert "mark_cancelled" in src
    assert "stop_foreground" in src
