"""What an autonomous agent has to say about its own work.

Reported: "often they fail a task but they lie and say they succeed."

Telling a model to be honest does nothing, because it does not think it is
lying. Three things in the old report formats produced this:

  1. Every slot presupposed success -- "what you did", "what you accomplished".
     There was nowhere to put what did NOT happen, so a narrative of activity
     was the only shape available and the reader inferred the rest.
  2. Failure was a conditional appendix ("if you could not complete it..."), so
     a partial success never classified itself as one and rounded up.
  3. Nothing ever asked HOW IT KNEW. A sub-agent that wrote a file and ran
     nothing had no reason not to call that done.

Pinned rather than eyeballed, and pinned across all four places, because the
rule is worth much less if it holds in three of them: the desktop's sub-agents,
the Browser Agent, and the phone's workers are the same kind of thing answering
to the same person.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from glmcode.prompts import (BROWSER_AGENT_SYSTEM, HONEST_REPORT_RULE,
                             SUBAGENT_PREAMBLE)

CORE_JS = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
needs_node = pytest.mark.skipif(
    not (shutil.which("node") and CORE_JS.is_file()),
    reason="node or mobile/agent-core.js unavailable")


def _phone(name):
    out = subprocess.run(
        ["node", "-e",
         f"const C=require(process.argv[1]);console.log(JSON.stringify(C.{name}));",
         str(CORE_JS)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


# --------------------------------------------------- the rule itself -----

def test_the_verdict_comes_first_and_is_one_of_three():
    """Leading with a verdict is what stops the drift: a model that has to
    choose DONE / PARTIAL / FAILED before it starts narrating cannot describe
    activity and leave success to be inferred."""
    first = HONEST_REPORT_RULE.strip().splitlines()[0]
    assert "DONE" in first and "PARTIAL" in first and "FAILED" in first


def test_each_verdict_is_defined_rather_than_named():
    for word in ("DONE", "PARTIAL", "FAILED"):
        assert f"- {word} --" in HONEST_REPORT_RULE, word


def test_done_requires_having_checked():
    """The whole of defect 3. Without this, "I made the edit" is DONE."""
    line = [ln for ln in HONEST_REPORT_RULE.splitlines() if ln.startswith("- DONE")][0]
    assert "checked it" in line


def test_partial_has_to_separate_what_is_from_what_is_not():
    line = [ln for ln in HONEST_REPORT_RULE.splitlines() if ln.startswith("- PARTIAL")][0]
    assert "is NOT" in line


def test_an_unverified_claim_has_to_carry_that_in_the_same_sentence():
    """Not a footnote at the end, which is read as a caveat on the whole report
    rather than on the one claim it belongs to."""
    assert "same sentence as the claim" in HONEST_REPORT_RULE


def test_intent_is_not_outcome():
    assert "INTENDED to do as what it does" in HONEST_REPORT_RULE


def test_it_says_outright_that_an_honest_partial_is_a_good_result():
    """The load-bearing sentence. The over-claim comes from believing failure
    is punished, so the rule has to say the opposite in as many words --
    everything above it is mechanism, and this is the reason."""
    assert "good result and costs you nothing" in HONEST_REPORT_RULE
    assert "actively damages the task" in HONEST_REPORT_RULE


# ------------------------------------------- it reaches all four places --

def test_the_desktop_subagent_carries_it():
    assert HONEST_REPORT_RULE in SUBAGENT_PREAMBLE


def test_the_browser_agent_carries_it():
    assert HONEST_REPORT_RULE in BROWSER_AGENT_SYSTEM


@needs_node
def test_the_phone_carries_the_same_words():
    """Word for word inside the prompt the phone's workers actually get, not a
    paraphrase -- a paraphrase is a different instruction with nothing to say
    so. Checked by containment rather than against an exported constant,
    matching how UNTRUSTED_INPUT_RULE is pinned: what matters is what reaches
    the model, not that the value exists somewhere in the module."""
    assert HONEST_REPORT_RULE in _phone("SUBAGENT_PROMPT")


# ------------------------------------ what counts as evidence, per place --

def test_the_placeholder_is_gone_by_the_time_anyone_formats_these():
    """Callers format these with their own fields, so a placeholder they know
    nothing about would raise KeyError on every single spawn."""
    assert "{honest}" not in SUBAGENT_PREAMBLE
    assert "{honest}" not in BROWSER_AGENT_SYSTEM
    SUBAGENT_PREAMBLE.format(name="n", task="t")
    BROWSER_AGENT_SYSTEM.format(goal="g")


def test_the_subagent_is_told_which_two_claims_matter_most():
    """Named specifically, because a model complies with a concrete
    prohibition far better than with a general call to be careful -- and these
    are the two the coordinator builds on."""
    body = SUBAGENT_PREAMBLE.lower()
    assert "unless you ran them in this session" in body
    assert "unless the write tool actually returned success" in body


def test_the_browser_agent_is_told_that_evidence_means_the_page_after():
    body = BROWSER_AGENT_SYSTEM
    assert "A click you did not see take effect did not take effect." in body
    assert "AFTER the action" in body


def test_the_browser_agent_is_told_being_blocked_is_normal():
    """Logins, captchas and paywalls are the ordinary weather of the job. A
    model that reads blocking as failure is a model that invents a result."""
    assert "ordinary weather" in BROWSER_AGENT_SYSTEM


@needs_node
def test_the_phone_says_done_is_rarely_honest_there():
    """There is no shell on a phone, so "and you checked it" is a bar it can
    almost never clear. Without this it reads the DONE definition, finds
    nothing it could have run, and uses DONE anyway."""
    body = _phone("SUBAGENT_PROMPT")
    assert "cannot run anything on this device" in body
    assert "DONE is rarely the honest verdict" in body


# ---------------------------------------------- and nothing regressed ----

def test_the_mission_is_still_the_last_thing_read():
    """A request ends on the turn. The report rules are long, and putting them
    after the mission would have the model answer THEM."""
    assert SUBAGENT_PREAMBLE.rstrip().endswith("{task}")
    assert BROWSER_AGENT_SYSTEM.rstrip().endswith("{goal}")
