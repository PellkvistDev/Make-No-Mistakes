"""A tool result must land on the chip its own call made.

Reported as a screenshot: a `browser_new_tab` chip, red, showing an error
about `run_command`.

Every path through `_handle_tool_calls` pairs a `tool_call` with a
`tool_result` -- except one. Arguments the model sent that will not parse as
JSON were answered with a reply and no call, because there was nothing to run.
The live UI attaches a result to the chip it built LAST, so that orphan landed
on the previous tool's chip: `finishToolEl` overwrites the body and adds the
error class, so a finished, successful call was relabelled with a different
tool's failure, under its own name. The failed call itself appeared nowhere.

Reloading the chat put it right, which is most of why this was hard to report:
`sessions.to_display` pairs by `tool_call_id` and was always correct. Only the
live stream was guessing.

So there are two things to hold: the orphan is gone, and the pairing no longer
depends on there not being one.
"""

import json

import pytest

from glmcode.agent import Agent
from glmcode.events import AgentEvents


class Recorder(AgentEvents):
    """Records the event stream the frontend would receive."""

    def __init__(self):
        self.events = []

    def tool_call(self, name, args, call_id=""):
        self.events.append(("tool_call", name, call_id))

    def tool_result(self, name, content, is_error=False, call_id=""):
        self.events.append(("tool_result", name, call_id, content, is_error))

    def ask_permission(self, *a, **k):
        raise AssertionError("no permission should be needed here")


def _agent(rec):
    a = Agent.__new__(Agent)
    a.events = rec
    a.transcript = None
    a.messages = [{"role": "assistant", "content": "", "tool_calls": []}]
    a.cancel = __import__("threading").Event()
    return a


def _call(cid, name, raw_args):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": raw_args}}


def test_unparseable_arguments_still_announce_the_call(monkeypatch):
    rec = Recorder()
    a = _agent(rec)
    a._handle_tool_calls([_call("c1", "browser_new_tab", '{"url": "https://x')])

    kinds = [e[0] for e in rec.events]
    assert kinds == ["tool_call", "tool_result"], (
        "a result with no call of its own lands on the previous tool's chip")
    assert rec.events[0][1] == rec.events[1][1] == "browser_new_tab"


def test_the_pair_agree_on_an_id(monkeypatch):
    """Not merely present -- the same on both halves, or matching by id is no
    better than matching by position."""
    rec = Recorder()
    a = _agent(rec)
    a._handle_tool_calls([_call("c1", "browser_new_tab", "{oh dear")])
    call, result = rec.events
    assert call[2] and call[2] == result[2]


def test_a_good_call_and_a_broken_one_do_not_share_a_chip(monkeypatch):
    """The reported shape: one tool succeeds, the next arrives unparseable.

    Both results carry ids, and they are different ids -- which is the whole
    of what stops the second one being written into the first one's box.
    """
    rec = Recorder()
    a = _agent(rec)
    monkeypatch.setattr(a, "_run_tool", lambda name, args, idx: "ok")

    class Allow:
        def check(self, *a, **k):
            class D:
                allowed, feedback = True, ""
            return D()
    a.permissions = Allow()
    a._turn_verified = False
    a._turn_wrote_files = False
    a._refine_pass_changed = False
    a._current_call_token = None

    a._handle_tool_calls([_call("c1", "run_command", '{"command": "ls"}'),
                          _call("c2", "browser_new_tab", "{broken")])

    ids = {}
    for e in rec.events:
        ids.setdefault(e[1], set()).add(e[2])
    assert len(ids["run_command"]) == 1
    assert len(ids["browser_new_tab"]) == 1
    assert ids["run_command"].isdisjoint(ids["browser_new_tab"])

    results = [e for e in rec.events if e[0] == "tool_result"]
    assert [(r[1], r[4]) for r in results] == [
        ("run_command", False), ("browser_new_tab", True)]


def test_the_replayed_chat_was_always_right():
    """Kept as the contrast: history pairs by tool_call_id, which is why a
    reload silently corrected what the live stream got wrong."""
    from glmcode import sessions
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            _call("c1", "run_command", '{"command": "ls"}'),
            _call("c2", "browser_new_tab", "{broken")]},
        {"role": "tool", "tool_call_id": "c2", "content": "ERROR: could not parse"},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    items = sessions.to_display(messages)
    tools = {i["name"]: i for i in items if i.get("kind") == "tool"}
    assert tools["run_command"]["result"] == "ok"
    assert "could not parse" in tools["browser_new_tab"]["result"]
    assert tools["browser_new_tab"]["error"] and not tools["run_command"]["error"]


# ---- the phone ran the tool anyway ----------------------------------------

import pathlib as _pathlib
import shutil as _shutil
import subprocess as _subprocess

_CORE = _pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
_needs_node = pytest.mark.skipif(
    not (_shutil.which("node") and _CORE.is_file()), reason="node unavailable")

_DRIVER = r"""
const C = require(process.argv[1]);
// One assistant turn holding a call whose arguments do not parse, then a
// plain answer so the loop ends.
let turn = 0;
const model = { chat: async () => (turn++ === 0
  ? { content: "", tool_calls: [{ id: "c1", type: "function",
      function: { name: "write_file", arguments: '{"path": "a.txt", "content": "oh' } }] }
  : { content: "done" }) };
const ran = [];
const tools = { write_file: async (args) => { ran.push(args); return "wrote"; } };
const events = [];
(async () => {
  await C.runAgent({ model, tools, messages: [{ role: "user", content: "go" }],
                     schemas: [], onEvent: (e) => events.push(e) });
  console.log(JSON.stringify({ ran, events: events.map((e) => [e.type, e.name || "", e.out || ""]) }));
})().catch((e) => console.log(JSON.stringify({ __err: String((e && e.stack) || e) })));
"""


@_needs_node
def test_the_phone_does_not_run_a_tool_with_arguments_it_could_not_read():
    """It swallowed the parse error and called the tool with {}. A write whose
    `content` was mangled became a write of nothing, and the only party who
    could have noticed -- the model -- was never told."""
    js = _subprocess.run(["node", "-e", _DRIVER, str(_CORE)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert js.returncode == 0, js.stderr
    out = json.loads(js.stdout)
    assert "__err" not in out, out.get("__err")
    assert out["ran"] == [], "the tool ran with arguments nobody could read"
    results = [e for e in out["events"] if e[0] == "tool_result"]
    assert results and "could not parse tool arguments" in results[0][2]
    assert [e[0] for e in out["events"] if e[0] in ("tool", "tool_result")] == \
           ["tool", "tool_result"]
