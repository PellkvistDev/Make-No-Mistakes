"""Workers dispatched from the phone's voice mode, driven through the real app.

The parity of the tool LIST is checked in tests/test_phone_workers.py. What
matters here is the behaviour those tools promise, and one property in
particular:

  dispatch_worker must RETURN before the work is done.

That is not a preference. The Live API's function calling is synchronous -- the
model is stopped until the tool returns -- so a dispatch that awaited the work
would hold the spoken conversation silent for the whole of it. The test proves
it by holding the worker's very first model call open and checking that the
dispatch has already answered.

The other property is the one the phone cannot escape: a worker runs in this
page, so a suspended app kills it. That is reported as an interruption, never
as a completion. "It finished" when it did not is the one outcome worse than
"it cannot run here", which is what this replaced.
"""

import json


def _to_chat(phone):
    p = phone.page
    p.fill("#in-model-key", "modelkey")
    p.fill("#in-gh-token", "ghtoken")
    p.fill("#in-pin", "1234")
    p.fill("#in-pin2", "1234")
    p.click("#btn-save-setup")
    p.wait_for_selector("#screen-repo:not([hidden])", timeout=15000)
    p.wait_for_selector(".repo-list li", timeout=15000)
    p.click(".repo-list li")
    p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)
    return phone


def _tool(phone, name, args=None, timeout_ms=15000):
    """Call a voice tool exactly as the Live socket's toolCall handler does.

    Raced against a timer, because the failure this file exists to catch is a
    tool that never returns -- and an un-raced evaluate() would express that as
    the suite hanging rather than as a test failing. A hang in CI reads as
    infrastructure trouble; this reads as what it is.
    """
    out = phone.page.evaluate(
        """async ([n, a, ms]) => {
             const slow = new Promise((r) => setTimeout(() => r("__TIMEOUT__"), ms));
             return Promise.race([window.__voiceTool(n, a), slow]);
           }""",
        [name, args or {}, timeout_ms])
    assert out != "__TIMEOUT__", (
        f"{name} did not return within {timeout_ms}ms. A Live function call "
        "holds the model silent until the tool returns, so this is the whole "
        "conversation going dead.")
    return out


def _seed_worker_reply(phone, answer="done that"):
    """The worker's model: one turn, no tool calls, then an answer."""
    phone.reply({"role": "assistant", "content": answer})


# ------------------------------------------------------- it does the work

def test_dispatch_starts_a_worker_and_says_so(phone):
    _to_chat(phone)
    _seed_worker_reply(phone)
    out = _tool(phone, "dispatch_worker", {"name": "tidy", "task": "tidy the readme"})
    assert "wk1" in out and "tidy" in out


def test_dispatch_returns_before_the_work_is_finished(phone):
    """The property the whole design rests on. Held open at the worker's first
    model call, the dispatch has already answered."""
    p = phone.page
    _to_chat(phone)
    _seed_worker_reply(phone)
    p.evaluate("() => { window.__holdNext = true; }")
    out = _tool(phone, "dispatch_worker", {"name": "slow", "task": "something long"})
    assert "wk1" in out, "dispatch must answer while the worker is still going"
    assert p.evaluate("() => window.__inFlight") is True, \
        "the worker's request should still be open at this point"
    # And it is honestly reported as still running.
    assert "running" in _tool(phone, "check_workers")
    p.evaluate("() => window.__release && window.__release()")
    p.wait_for_timeout(500)


def test_a_worker_that_finishes_is_reported_as_done(phone):
    p = phone.page
    _to_chat(phone)
    _seed_worker_reply(phone, "tidied it")
    _tool(phone, "dispatch_worker", {"name": "tidy", "task": "tidy the readme"})
    p.wait_for_function(
        "async () => /done/.test(await window.__voiceTool('check_workers', {}))",
        timeout=20000)
    assert "tidied it" in _tool(phone, "check_workers")


def test_a_worker_can_actually_change_a_file(phone):
    """Read-only was the old ceiling; this is the point of lifting it."""
    p = phone.page
    _to_chat(phone)
    phone.reply(
        {"role": "assistant", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": "NOTES.md", "content": "hi"})}}]},
        {"role": "assistant", "content": "wrote it"})
    _tool(phone, "dispatch_worker", {"name": "note", "task": "write NOTES.md"})
    p.wait_for_function(
        "async () => /done/.test(await window.__voiceTool('check_workers', {}))",
        timeout=20000)
    assert "NOTES.md" in _tool(phone, "worker_changes", {"worker": "wk1"})
    assert p.evaluate("() => !!window.__files['NOTES.md']") is True


def test_a_worker_can_be_undone(phone):
    p = phone.page
    _to_chat(phone)
    p.evaluate("() => window.__seedFile('NOTES.md', 'original')")
    phone.reply(
        {"role": "assistant", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "write_file",
                         "arguments": json.dumps({"path": "NOTES.md", "content": "replaced"})}}]},
        {"role": "assistant", "content": "changed it"})
    _tool(phone, "dispatch_worker", {"name": "edit", "task": "edit NOTES.md"})
    p.wait_for_function(
        "async () => /done/.test(await window.__voiceTool('check_workers', {}))",
        timeout=20000)
    out = _tool(phone, "revert_worker", {"worker": "wk1"})
    assert "Reverted 1" in out
    body = p.evaluate("() => atob(window.__files['NOTES.md'].content)")
    assert body == "original", "revert has to restore what was there before"


# ------------------------------------------------------- steering and stopping

def test_a_running_worker_can_be_steered(phone):
    p = phone.page
    _to_chat(phone)
    _seed_worker_reply(phone)
    p.evaluate("() => { window.__holdNext = true; }")
    _tool(phone, "dispatch_worker", {"name": "w", "task": "do a thing"})
    out = _tool(phone, "steer_worker", {"worker": "wk1", "message": "also do the other thing"})
    assert "wk1" in out
    p.evaluate("() => window.__release && window.__release()")
    p.wait_for_timeout(500)


def test_a_running_worker_can_be_stopped(phone):
    p = phone.page
    _to_chat(phone)
    _seed_worker_reply(phone)
    p.evaluate("() => { window.__holdNext = true; }")
    _tool(phone, "dispatch_worker", {"name": "w", "task": "do a thing"})
    assert "Stopping wk1" in _tool(phone, "stop_worker", {"worker": "wk1"})
    p.evaluate("() => window.__release && window.__release()")
    p.wait_for_function(
        "async () => /stopped|done/.test(await window.__voiceTool('check_workers', {}))",
        timeout=20000)


def test_workers_answer_to_their_name_as_well_as_their_id(phone):
    """The model is told it can use either, and a user says the name."""
    _to_chat(phone)
    _seed_worker_reply(phone)
    _tool(phone, "dispatch_worker", {"name": "dark-mode", "task": "add a dark theme"})
    assert "ERROR" not in _tool(phone, "steer_worker",
                                {"worker": "dark-mode", "message": "use CSS variables"})


def test_an_unknown_worker_is_an_error_the_model_can_say_out_loud(phone):
    _to_chat(phone)
    assert "ERROR" in _tool(phone, "stop_worker", {"worker": "wk9"})
    assert "No workers" in _tool(phone, "check_workers")


# ----------------------------------------------- the limit, reported honestly

def test_a_worker_killed_by_the_app_going_away_says_so(phone):
    """The one thing the phone cannot engineer around: the page is the runtime,
    and a hidden tab gets its request killed. Reported as interrupted -- never
    as finished, which is the only outcome worse than not being able to run it
    at all."""
    p = phone.page
    _to_chat(phone)
    _seed_worker_reply(phone)
    p.evaluate("() => { window.__holdNext = true; }")
    _tool(phone, "dispatch_worker", {"name": "doomed", "task": "long job"})
    # The app goes away, and the request in flight dies with it.
    p.evaluate("""() => {
      Object.defineProperty(document, 'hidden', { get: () => true, configurable: true });
      window.__failNext = true;
      window.__release && window.__release();
    }""")
    p.wait_for_function(
        "async () => /interrupted/.test(await window.__voiceTool('check_workers', {}))",
        timeout=20000)
    said = _tool(phone, "check_workers")
    assert "interrupted" in said
    assert "backgrounded" in said, "it has to say WHY, or it reads as a random failure"
    assert "done" not in said
