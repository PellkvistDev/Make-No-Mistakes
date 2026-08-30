"""The mistake ledger: what goes wrong here, counted.

The properties pinned below are the ones the feature is *for*. Two of them are
the whole reason it is not just a log file:

  - a success DECAYS a pattern and never deletes it, so the evidence that
    something was once a problem outlives the problem;
  - the block that reaches the system prompt is capped, because it competes
    with ~12,400 tokens of prefix that a growing block would stop caching.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from glmcode import ledger
from glmcode.agent import Agent


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A ledger of its own per test. Both names are patched: _write calls
    CONFIG_DIR.mkdir, so pointing only LEDGER_FILE at tmp_path would still
    create the real config dir on a machine that has none."""
    f = tmp_path / "ledger.json"
    monkeypatch.setattr(ledger, "LEDGER_FILE", f)
    monkeypatch.setattr(ledger, "CONFIG_DIR", tmp_path)
    return f


PROJ = "/proj"
MODEL = "flash"
ENDPOINT = "https://api.example.com/v1"


def fail(n=1, tool="edit_file", error="ERROR: old_string not found in a.py",
         project=PROJ, model=MODEL, endpoint=ENDPOINT, warned=False):
    for _ in range(n):
        ledger.record_failure(project, model, endpoint, tool, error,
                              was_warned=warned)


# --------------------------------------------------------------------- #
# Signatures: the thing that turns a pile of errors into a count

def test_same_mistake_about_different_files_is_one_pattern():
    a = ledger.signature("edit_file", "ERROR: old_string not found in src/a.py")
    b = ledger.signature("edit_file", "ERROR: old_string not found in lib/deep/b.py")
    assert a == b


def test_numbers_shas_and_quotes_are_normalised_away():
    a = ledger.signature("write_file", 'ERROR: 422 sha "abc1234def" is stale at line 91')
    b = ledger.signature("write_file", 'ERROR: 500 sha "99ffee0011" is stale at line 7')
    assert a == b
    assert "<n>" in a and "<str>" in a


def test_different_tools_never_collapse_together():
    same_text = "ERROR: not found"
    assert (ledger.signature("edit_file", same_text)
            != ledger.signature("run_command", same_text))


def test_genuinely_different_failures_stay_apart():
    a = ledger.signature("run_command", "ERROR: command not found: pytest")
    b = ledger.signature("run_command", "ERROR: permission denied")
    assert a != b


# --------------------------------------------------------------------- #
# Thresholds

def test_one_failure_is_a_coincidence_and_earns_no_rule(store):
    fail(1)
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == ""


def test_three_failures_become_a_rule(store):
    fail(ledger.MIN_HITS)
    block = ledger.rules_block(PROJ, MODEL, ENDPOINT)
    assert "edit_file" in block
    assert "Mistakes you have actually made here" in block


def test_advice_is_silent_the_first_time_and_speaks_the_second(store):
    fail(1)
    assert ledger.advice_for(PROJ, MODEL, ENDPOINT, "edit_file",
                             "ERROR: old_string not found in a.py") == ""
    fail(1)
    advice = ledger.advice_for(PROJ, MODEL, ENDPOINT, "edit_file",
                               "ERROR: old_string not found in a.py")
    assert "2 times" in advice


# --------------------------------------------------------------------- #
# Decay -- the load-bearing one

def test_success_decays_a_pattern_but_never_deletes_it(store):
    fail(ledger.MIN_HITS)
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) != ""

    # Enough successes to drop confidence below the injection threshold.
    for _ in range(10):
        ledger.record_success(PROJ, MODEL, ENDPOINT, "edit_file")

    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == "", \
        "a tool that works again should stop lecturing the model"

    # ...but the record is still there. Losing it would destroy the evidence
    # that this was ever a problem, which is the sync-index failure again.
    rows = ledger.report(PROJ)
    row = next(r for r in rows if r["tool"] == "edit_file")
    assert row["failures"] == ledger.MIN_HITS
    assert row["successes"] == 10
    assert row["active"] is False


def test_success_on_one_tool_does_not_decay_another(store):
    fail(ledger.MIN_HITS, tool="edit_file")
    fail(ledger.MIN_HITS, tool="run_command", error="ERROR: no such flag")
    for _ in range(10):
        ledger.record_success(PROJ, MODEL, ENDPOINT, "edit_file")
    block = ledger.rules_block(PROJ, MODEL, ENDPOINT)
    assert "edit_file" not in block
    assert "run_command" in block


# --------------------------------------------------------------------- #
# Keyed on model AND endpoint

def test_another_model_does_not_inherit_these_lessons(store):
    fail(ledger.MIN_HITS)
    assert ledger.rules_block(PROJ, "other-model", ENDPOINT) == ""


def test_the_same_model_on_another_provider_is_a_different_bucket(store):
    fail(ledger.MIN_HITS)
    assert ledger.rules_block(PROJ, MODEL, "https://elsewhere.example/v1") == ""


def test_another_project_does_not_inherit_these_lessons(store):
    fail(ledger.MIN_HITS)
    assert ledger.rules_block("/other", MODEL, ENDPOINT) == ""


def test_one_provider_is_not_split_by_its_url_path(store):
    """Keyed on the host, not the whole URL: /v1 and /v1beta/openai are the
    same provider and splitting them would halve its history for no reason."""
    fail(ledger.MIN_HITS, endpoint="https://api.example.com/v1")
    assert ledger.rules_block(PROJ, MODEL,
                              "https://api.example.com/v1beta/openai") != ""


# --------------------------------------------------------------------- #
# The cap -- it competes with the prompt prefix

def test_the_block_is_capped_in_rules_and_in_characters(store):
    for i in range(ledger.MAX_RULES + 8):
        fail(ledger.MIN_HITS, tool=f"tool_{i}",
             error=f"ERROR: distinct failure kind {'x' * 400} number-{i}-word")
    block = ledger.rules_block(PROJ, MODEL, ENDPOINT)
    assert block.count("\n- ") <= ledger.MAX_RULES
    assert len(block) < ledger.MAX_RULE_CHARS + 800   # + the fixed header


def test_a_bucket_is_bounded_so_the_file_cannot_grow_forever(store):
    for i in range(ledger.MAX_PATTERNS_PER_BUCKET + 25):
        fail(1, tool="run_command", error=f"ERROR: unique-kind-{i}-alpha")
    rows = ledger.report(PROJ)
    assert len(rows) <= ledger.MAX_PATTERNS_PER_BUCKET


# --------------------------------------------------------------------- #
# Was the model already warned?

def test_a_rule_that_keeps_firing_says_so_instead_of_repeating_itself(store):
    fail(ledger.MIN_HITS)
    fail(2, warned=True)
    block = ledger.rules_block(PROJ, MODEL, ENDPOINT)
    assert "re-reading it is not the fix" in block
    assert ledger.report(PROJ)[0]["warned_and_failed"] == 2


def test_warned_count_is_reported_for_the_panel(store):
    fail(ledger.MIN_HITS)
    assert ledger.report(PROJ)[0]["warned_and_failed"] == 0


# --------------------------------------------------------------------- #
# Never raises

def test_a_corrupt_file_is_survived_not_propagated(store):
    store.write_text("{ this is not json", encoding="utf-8")
    fail(1)                                   # must not raise
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == ""
    assert ledger.report(PROJ) == [] or isinstance(ledger.report(PROJ), list)


def test_a_ledger_that_cannot_be_written_never_breaks_a_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(ledger, "LEDGER_FILE", tmp_path / "nope" / "x.json")
    monkeypatch.setattr(ledger, "CONFIG_DIR", tmp_path / "nope")

    def boom(*a, **k):
        raise OSError("read-only filesystem")
    monkeypatch.setattr(ledger.Path, "mkdir", boom)
    fail(1)                                   # must not raise
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == ""


def test_forget_clears_only_the_project_asked_for(store):
    fail(ledger.MIN_HITS)
    fail(ledger.MIN_HITS, project="/other")
    ledger.forget(PROJ)
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == ""
    assert ledger.rules_block("/other", MODEL, ENDPOINT) != ""


# --------------------------------------------------------------------- #
# The agent side: one funnel, and what must stay out of it

class _Events:
    def __init__(self):
        self.results = []

    def tool_result(self, name, content, is_error=False, call_id=""):
        self.results.append((name, content, is_error))


def _agent(tmp_path, enabled=True):
    """Not a real Agent: _record_outcome and _tool_reply use exactly these
    attributes, and building a real one would drag in a provider client."""
    return SimpleNamespace(
        workdir=Path(PROJ),
        cfg=SimpleNamespace(model=MODEL, learn_from_mistakes=enabled),
        model_override="",
        messages=[],
        transcript=None,
        events=_Events(),
        rebuilt=0,
        _chat_base_url=lambda: ENDPOINT,
        rebuild_system_prompt=lambda: None,
    )


def test_a_repeat_failure_reaches_the_model_with_the_count_attached(store, tmp_path):
    a = _agent(tmp_path)
    err = "ERROR: old_string not found in a.py"
    first = Agent._record_outcome(a, "edit_file", err, True)
    assert "[Ledger]" not in first
    second = Agent._record_outcome(a, "edit_file", err, True)
    assert "[Ledger]" in second and "2 times" in second


def test_a_successful_result_is_never_rewritten(store, tmp_path):
    a = _agent(tmp_path)
    out = Agent._record_outcome(a, "read_file", "  1 | hello\n", False)
    assert out == "  1 | hello\n"


def test_a_permission_denial_is_not_a_mistake_the_model_made(store, tmp_path):
    """It arrives at _tool_reply as an error and is not one -- the user said
    no. Recording it would teach the model the tool is broken, and would put
    the user's own decisions in a block titled "mistakes you have made"."""
    a = _agent(tmp_path)
    tc = {"id": "call-1"}
    for _ in range(ledger.MIN_HITS + 2):
        Agent._tool_reply(a, tc, "User denied permission for this tool call.",
                          error=True, name="run_command", args={}, learn=False)
    assert ledger.rules_block(PROJ, MODEL, ENDPOINT) == ""
    assert ledger.report(PROJ) == []


def test_turning_it_off_records_nothing(store, tmp_path):
    a = _agent(tmp_path, enabled=False)
    for _ in range(ledger.MIN_HITS + 2):
        Agent._record_outcome(a, "edit_file", "ERROR: old_string not found", True)
    assert ledger.report(PROJ) == []


def test_a_new_rule_takes_effect_in_the_conversation_that_earned_it(store, tmp_path):
    """Same reason the remember tool rebuilds the prompt: a lesson that only
    applies to the NEXT chat is no use to the turn that just learned it."""
    a = _agent(tmp_path)
    rebuilt = []
    a.rebuild_system_prompt = lambda: rebuilt.append(1)
    err = "ERROR: old_string not found in a.py"
    for _ in range(ledger.MIN_HITS):
        Agent._record_outcome(a, "edit_file", err, True)
    assert rebuilt, "crossing into a rule must refresh the system prompt"
    before = len(rebuilt)
    Agent._record_outcome(a, "edit_file", err, True)
    assert len(rebuilt) == before, "an unchanged rule set must not rebuild again"


# --------------------------------------------------------------------- #
# The prompt

def test_the_block_reaches_the_system_prompt(store):
    from glmcode.prompts import build_system_prompt
    fail(ledger.MIN_HITS, project=Path.cwd())
    prompt = build_system_prompt(Path.cwd(), MODEL, endpoint=ENDPOINT)
    assert "Mistakes you have actually made here" in prompt
    assert "edit_file" in prompt


def test_learn_false_leaves_the_prompt_alone(store):
    from glmcode.prompts import build_system_prompt
    fail(ledger.MIN_HITS, project=Path.cwd())
    prompt = build_system_prompt(Path.cwd(), MODEL, endpoint=ENDPOINT, learn=False)
    assert "Mistakes you have actually made here" not in prompt


def test_an_empty_ledger_adds_nothing_to_the_prompt(store):
    from glmcode.prompts import build_system_prompt
    prompt = build_system_prompt(Path.cwd(), MODEL, endpoint=ENDPOINT)
    assert "Mistakes you have actually made here" not in prompt


def test_it_never_raises_on_an_agent_that_was_never_initialised(store):
    """Several tests drive _handle_tool_calls on an Agent built with __new__
    and no __init__, so there is no self.cfg. The first version read the
    config OUTSIDE the try -- so the one line whose whole job was to turn this
    feature off became the line that brought the turn down."""
    bare = Agent.__new__(Agent)
    assert Agent._record_outcome(bare, "edit_file", "ERROR: boom", True) == "ERROR: boom"
    assert Agent._record_outcome(bare, "read_file", "fine", False) == "fine"
