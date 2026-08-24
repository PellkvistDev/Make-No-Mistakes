"""Work started by speaking is inspectable, and a sub-agent can ask.

Two halves of one report: "there are problems with the connection between the
voice agents and the text agents -- no context works. And when the text agent
spawns agents, it doesn't work."

The rail's worker pills were already buttons that open the sub-agent inspector
-- a worker's id IS its sub-agent id, so there was nothing to build. What they
opened was empty. handleVoiceEvent dropped `subagent` and `subagent_stream` on
the floor, so a worker dispatched by speaking never had a thread created and
never had its status recorded: the panel came up blank with a dead composer, on
the worker you are most likely to want to redirect.

The permission card is the other half. It was a single slot -- `permId =
ev.id` -- which is right for the main agent (one thread, blocks on the answer)
and wrong for sub-agents, which run several at once and each block only
themselves. A second request overwrote the first, and the thread behind it sat
until its five-minute timeout with nothing on screen to answer.
"""

from .conftest import DEFAULT_SETTINGS


def _app(desktop, **replies):
    desktop.page.set_viewport_size({"width": 1400, "height": 900})
    desktop.boot(boot={"settings": dict(DEFAULT_SETTINGS)}, **replies)
    desktop.page.evaluate("""() => applySession({
      id: "s1", cwd: "/tmp/p", items: [], todos: [], sessions: [],
      prompt_tokens: 0, completion_tokens: 0, context: 0 })""")
    desktop.page.wait_for_timeout(200)
    return desktop


def _voice(desktop, ev):
    """An event on the delegator's own sink, the way WebEvents stamps it.

    The dock is revealed too: a live session has it up, and #voice-perm is
    inside it -- asserting against a card whose ancestor is hidden would pass
    or fail for the wrong reason."""
    desktop.page.evaluate("""(ev) => {
      voice.active = true;
      document.getElementById('voice-dock').hidden = false;
      renderActivityRail();
      ev.sid = (window.currentSid || "s1") + "::voice";
      window.GLM.emit(ev);
    }""", ev)
    desktop.page.wait_for_timeout(150)


def _typed(desktop, ev):
    desktop.page.evaluate("(ev) => window.GLM.emit(ev)", ev)
    desktop.page.wait_for_timeout(150)


# ------------------------------------- a spoken worker's own thread -------

def test_a_worker_dispatched_by_voice_has_a_thread_to_look_at(desktop):
    _app(desktop)
    _voice(desktop, {"type": "worker_update", "id": "wk1",
                     "name": "dark-mode", "status": "started", "summary": ""})
    _voice(desktop, {"type": "subagent", "id": "wk1", "name": "dark-mode",
                     "status": "running", "mission": "add a dark mode"})
    _voice(desktop, {"type": "subagent_stream", "id": "wk1", "kind": "tool_call",
                     "name": "edit_file", "args": {"path": "style.css"}})
    desktop.page.evaluate("() => openSubagentPanel('wk1', 'dark-mode', 'running')")
    desktop.page.wait_for_timeout(200)
    body = desktop.page.text_content("#subagent-panel-body")
    assert "style.css" in body, f"the panel came up empty: {body!r}"
    assert desktop.errors == []


def test_its_status_reaches_the_panel_so_steering_is_possible(desktop):
    """Visible-but-un-steerable is the worse failure: it looks like it worked.
    The composer is gated on subagentStatus, which the dropped `subagent` event
    was the only thing that set."""
    _app(desktop)
    _voice(desktop, {"type": "subagent", "id": "wk1", "name": "dark-mode",
                     "status": "running", "mission": "add a dark mode"})
    desktop.page.evaluate("() => openSubagentPanel('wk1', 'dark-mode', 'running')")
    desktop.page.wait_for_timeout(200)
    assert desktop.page.eval_on_selector("#subagent-input", "e => e.disabled") is False


def test_a_finished_spoken_worker_closes_its_composer(desktop):
    _app(desktop)
    _voice(desktop, {"type": "subagent", "id": "wk1", "name": "dark-mode",
                     "status": "running", "mission": "m"})
    desktop.page.evaluate("() => openSubagentPanel('wk1', 'dark-mode', 'running')")
    _voice(desktop, {"type": "subagent", "id": "wk1", "name": "dark-mode",
                     "status": "done", "summary": "did it"})
    desktop.page.evaluate("() => updateSubagentComposerState()")
    assert desktop.page.eval_on_selector("#subagent-input", "e => e.disabled") is True


def test_a_spoken_dispatch_does_not_draw_a_row_into_the_chat(desktop):
    """There is no turn to hang one on, and the rail already shows it."""
    _app(desktop)
    _voice(desktop, {"type": "subagent", "id": "wk1", "name": "dark-mode",
                     "status": "running", "mission": "m"})
    assert desktop.page.query_selector("#chat .subagent-row") is None


# --------------------------------------------- the permission card --------

def _perm(desktop, rid, worker, title):
    _typed(desktop, {"type": "worker_permission", "sid": "s1", "rid": rid,
                     "worker": worker, "title": title, "preview": "x",
                     "always": ""})


def test_a_spawned_subagent_can_be_answered_at_all(desktop):
    """It only ever reached the voice dock, so a sub-agent that hit a gated
    action while typing had nowhere to be answered from."""
    _app(desktop)
    _perm(desktop, "r1", "research", "write notes.md")
    assert desktop.page.is_visible("#perm-backdrop")
    assert "research" in desktop.page.text_content("#perm-title")


def test_the_answer_goes_to_the_worker_call_not_the_main_one(desktop):
    """The two are keyed differently -- rid in the worker registry, id in the
    events sink -- so the card carries which kind it is holding."""
    _app(desktop)
    _perm(desktop, "r1", "research", "write notes.md")
    desktop.page.click("#perm-allow")
    desktop.page.wait_for_timeout(150)
    calls = desktop.calls("resolve_worker_permission")
    assert calls and calls[0]["args"][0] == "r1"
    assert desktop.calls("permission_response") == []


def test_two_subagents_asking_together_both_get_answered(desktop):
    """The single slot lost the first one, and its thread sat until timeout."""
    _app(desktop)
    _perm(desktop, "r1", "research", "write notes.md")
    _perm(desktop, "r2", "tests", "run pytest")
    desktop.page.click("#perm-allow")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_visible("#perm-backdrop"), "the second question vanished"
    assert "tests" in desktop.page.text_content("#perm-title")
    desktop.page.click("#perm-deny")
    desktop.page.wait_for_timeout(150)
    answered = [c["args"][0] for c in desktop.calls("resolve_worker_permission")]
    assert answered == ["r1", "r2"]


def test_the_card_closes_when_the_queue_empties(desktop):
    _app(desktop)
    _perm(desktop, "r1", "research", "write notes.md")
    desktop.page.click("#perm-allow")
    desktop.page.wait_for_timeout(150)
    assert desktop.page.is_hidden("#perm-backdrop")


def test_the_main_agents_own_question_still_works(desktop):
    """The queue serves both sources; the one that was already there must not
    have been traded for the new one."""
    _app(desktop)
    _typed(desktop, {"type": "permission", "sid": "s1", "id": "p1",
                     "title": "write app.py", "preview": "x", "always": ""})
    assert desktop.page.is_visible("#perm-backdrop")
    desktop.page.click("#perm-allow")
    desktop.page.wait_for_timeout(150)
    calls = desktop.calls("permission_response")
    assert calls and calls[0]["args"][0] == "p1"


def test_a_spoken_worker_is_still_answered_out_loud(desktop):
    """In a spoken conversation the question stays on the dock's card -- it is
    answerable by saying yes, and a modal over the app is not."""
    _app(desktop)
    _voice(desktop, {"type": "worker_permission", "rid": "r1", "worker": "dark-mode",
                     "title": "write style.css", "preview": "x", "always": ""})
    assert desktop.page.is_hidden("#perm-backdrop")
    assert desktop.page.is_visible("#voice-perm")


# ------------------------------- a typed worker's result is filed too -----

def test_a_worker_dispatched_by_typing_reports_back(desktop):
    """The mirror of the registry bug, on the reporting side. announceQ is the
    voice route's, so a worker started by typing finished on its own daemon
    thread and the agent you were typing to was never told -- you had to hope
    it thought to call check_workers."""
    _app(desktop)
    _typed(desktop, {"type": "worker_update", "sid": "s1", "id": "wk1",
                     "name": "dark-mode", "status": "done",
                     "summary": "did it", "result": "Edited style.css"})
    calls = desktop.calls("record_worker_result")
    assert calls, "the result went nowhere"
    assert calls[0]["args"][:2] == ["dark-mode", "done"]


def test_a_stopped_one_is_filed_as_stopped(desktop):
    """Telling the agent a cancelled job crashed invites it to diagnose and
    re-run exactly what was just cancelled."""
    _app(desktop)
    _typed(desktop, {"type": "worker_update", "sid": "s1", "id": "wk1",
                     "name": "dark-mode", "status": "stopped",
                     "summary": "", "result": ""})
    calls = desktop.calls("record_worker_result")
    assert calls and calls[0]["args"][1] == "stopped"


def test_starting_one_files_nothing(desktop):
    """"started" and "running" are not results."""
    _app(desktop)
    _typed(desktop, {"type": "worker_update", "sid": "s1", "id": "wk1",
                     "name": "dark-mode", "status": "started", "summary": ""})
    assert desktop.calls("record_worker_result") == []


def test_a_spoken_worker_is_not_filed_twice(desktop):
    """A worker reports on the sink of whoever dispatched it -- one route each
    -- and the voice route files through its own announce path."""
    _app(desktop)
    _voice(desktop, {"type": "worker_update", "id": "wk1", "name": "dark-mode",
                     "status": "done", "summary": "did it", "result": "r"})
    assert len(desktop.calls("record_worker_result")) <= 1
