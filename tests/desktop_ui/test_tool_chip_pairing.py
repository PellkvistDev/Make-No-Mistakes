"""A result renders in the box belonging to its own call.

The page attached every `tool_result` to the chip it had built LAST. That is
correct while calls and results strictly alternate, and it produced the
reported screenshot the moment they did not: a `browser_new_tab` chip, red,
carrying an error about `run_command`.

These drive the real page with the real markup, because the whole defect lives
in which DOM node got written to.
"""

from .conftest import DEFAULT_SETTINGS


def _app(desktop):
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)})
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/tmp/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.wait_for_timeout(150)
    return desktop


def _emit(desktop, *events):
    desktop.page.evaluate("""(evs) => {
      for (const e of evs) { e.sid = window.currentSid || "s1"; window.GLM.emit(e); }
    }""", list(events))
    desktop.page.wait_for_timeout(150)


def _chips(desktop):
    return desktop.page.evaluate("""() => [...document.querySelectorAll('#chat .tool')].map((el) => ({
      name: el.querySelector('.tool-name').textContent,
      state: el.querySelector('.tool-state').textContent,
      body: el.querySelector('.tool-body').textContent,
      error: el.classList.contains('error'),
    }))""")


def _turn(desktop):
    """A turn to hang chips on -- tool events need one open."""
    _emit(desktop, {"type": "stream_start"})


def test_two_calls_two_boxes(desktop):
    _app(desktop)
    _turn(desktop)
    _emit(desktop,
          {"type": "tool_call", "name": "run_command", "args": {"command": "ls"}, "call_id": "a"},
          {"type": "tool_result", "name": "run_command", "content": "file.txt", "call_id": "a"},
          {"type": "tool_call", "name": "browser_new_tab", "args": {}, "call_id": "b"},
          {"type": "tool_result", "name": "browser_new_tab", "content": "opened", "call_id": "b"})
    got = _chips(desktop)
    assert [c["name"] for c in got] == ["run_command", "browser_new_tab"]
    assert [c["body"] for c in got] == ["file.txt", "opened"]
    assert desktop.errors == []


def test_a_result_out_of_order_does_not_overwrite_the_other_box(desktop):
    """The bug, reduced: two calls open, and the SECOND result arrives first.

    Matching on "the chip I built last" puts it in the wrong box and leaves
    the other spinning for good.
    """
    _app(desktop)
    _turn(desktop)
    _emit(desktop,
          {"type": "tool_call", "name": "run_command", "args": {"command": "ls"}, "call_id": "a"},
          {"type": "tool_call", "name": "browser_new_tab", "args": {}, "call_id": "b"},
          {"type": "tool_result", "name": "run_command",
           "content": "ERROR: no such file", "error": True, "call_id": "a"},
          {"type": "tool_result", "name": "browser_new_tab", "content": "opened", "call_id": "b"})
    got = {c["name"]: c for c in _chips(desktop)}
    assert got["run_command"]["error"] and "no such file" in got["run_command"]["body"]
    assert not got["browser_new_tab"]["error"]
    assert got["browser_new_tab"]["body"] == "opened"
    assert got["browser_new_tab"]["state"] == "done", "left running forever"
    assert desktop.errors == []


def test_a_result_with_no_id_still_lands_somewhere(desktop):
    """An older backend, or a replayed event. Falling back to the last chip is
    what the page always did; it must keep working rather than dropping the
    result on the floor."""
    _app(desktop)
    _turn(desktop)
    _emit(desktop,
          {"type": "tool_call", "name": "read_file", "args": {"path": "a.py"}},
          {"type": "tool_result", "name": "read_file", "content": "1 | x = 1"})
    got = _chips(desktop)
    assert len(got) == 1 and got[0]["body"] == "1 | x = 1"
    assert desktop.errors == []
