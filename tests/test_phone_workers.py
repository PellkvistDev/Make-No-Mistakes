"""The phone's voice mode can do the work, not just talk about it.

A spoken session on the phone used to get the read tools and needs_desktop, so
asking it to do anything left a note for the computer instead of doing it. The
reasoning behind that was sound as a DEFAULT -- the phone genuinely cannot work
in the background, and a commit you cannot see the diff of, triggered by a
sentence that might have been misheard, is a real risk. It is the user's risk
to take, though, and being told "I've written that down for your computer" when
you asked for work done is its own kind of broken.

So the phone now offers what the desktop's voice mode offers. The tool
descriptions are what the model actually reads, so they are pinned against the
desktop's rather than paraphrased: a description that drifts changes how the
tool gets used, with nothing failing to say so.

What is NOT claimed anywhere: that a worker survives the app being closed. It
runs in the page, and the page is what iOS suspends. The honest handling of
that is tested in tests/mobile_ui/test_voice_workers.py.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from glmcode.tools import CONVERSATIONAL_SCHEMAS

CORE_JS = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
needs_node = pytest.mark.skipif(
    not (shutil.which("node") and CORE_JS.is_file()),
    reason="node or mobile/agent-core.js unavailable")


def _phone():
    out = subprocess.run(
        ["node", "-e",
         "const C=require(process.argv[1]);"
         "console.log(JSON.stringify(C.WORKER_SCHEMAS));", str(CORE_JS)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return {s["function"]["name"]: s["function"] for s in json.loads(out.stdout)}


def _desktop():
    return {s["function"]["name"]: s["function"] for s in CONVERSATIONAL_SCHEMAS}


@needs_node
def test_the_phone_offers_every_worker_tool_the_desktop_does():
    """The whole point. A tool the desktop's voice has and the phone's does not
    is a thing you can ask for at your computer and not from your pocket."""
    assert set(_phone()) == set(_desktop())


@needs_node
@pytest.mark.parametrize("name", sorted(_desktop()))
def test_the_description_matches_the_desktop_word_for_word(name):
    """The description IS the interface here -- it is what decides whether the
    model dispatches a worker or tries to do the job inline. Paraphrasing it on
    one device makes the two behave differently for no visible reason."""
    assert _phone()[name]["description"] == _desktop()[name]["description"]


@needs_node
@pytest.mark.parametrize("name", sorted(_desktop()))
def test_the_arguments_match_the_desktop(name):
    """A model that has learned 'worker' on one device must not find it called
    something else on the other."""
    theirs = _phone()[name].get("parameters") or {}
    ours = _desktop()[name].get("parameters") or {}
    assert (theirs.get("properties") or {}) == (ours.get("properties") or {})
    assert sorted(theirs.get("required") or []) == sorted(ours.get("required") or [])


@needs_node
def test_dispatch_still_promises_to_return_instantly():
    """Not cosmetic. The Live API's function calling is synchronous -- the model
    is stopped until the tool returns -- so a dispatch that waited for the work
    would hold the conversation silent for the whole of it. The phone's
    implementation returns before the work is done, and the description is what
    tells the model it can carry on talking."""
    d = _phone()["dispatch_worker"]["description"]
    assert "WITHOUT blocking" in d
    assert "Returns instantly" in d or "returns instantly" in d
