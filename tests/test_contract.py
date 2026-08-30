"""Intent contracts: what the turn was for, written where it survives.

The checkable half is what these mostly pin. "Did it achieve the goal" is a
judgement this module does not pretend to make; "did it change something it
promised not to" is a set difference, and that one has to be right.
"""

from types import SimpleNamespace

import pytest

from glmcode import contract as C
from glmcode.agent import Agent
from glmcode.errors import ToolError


# --------------------------------------------------------------------- #
# Matching

def test_an_exact_path_is_matched():
    c = C.make(must_not_change=["src/api/schema.py"])
    assert C.violations(c, ["src/api/schema.py"], "/proj")


def test_a_directory_covers_what_is_inside_it():
    """fnmatch does not do this, and somebody writing a directory name and
    getting no protection from it is the worst way for this to be wrong: it
    reads as covered."""
    c = C.make(must_not_change=["tests"])
    assert C.violations(c, ["tests/test_a.py"], "/proj")
    assert C.violations(c, ["tests/deep/test_b.py"], "/proj")


def test_a_trailing_slash_behaves_the_same_way():
    c = C.make(must_not_change=["glmcode/gui/"])
    assert C.violations(c, ["glmcode/gui/app.py"], "/proj")


def test_a_glob_is_matched():
    c = C.make(must_not_change=["*.lock"])
    assert C.violations(c, ["poetry.lock"], "/proj")
    assert not C.violations(c, ["poetry.toml"], "/proj")


def test_a_near_miss_directory_is_not_matched():
    """"tests" must not cover "tests-old", the string-prefix bug this repo has
    already been bitten by in path containment."""
    c = C.make(must_not_change=["tests"])
    assert not C.violations(c, ["tests-old/thing.py"], "/proj")


def test_untouched_files_are_not_reported():
    c = C.make(must_not_change=["tests"])
    assert C.violations(c, ["src/main.py"], "/proj") == []


def test_each_path_is_reported_once_even_with_overlapping_patterns():
    c = C.make(must_not_change=["tests", "tests/test_a.py"])
    hits = C.violations(c, ["tests/test_a.py"], "/proj")
    assert len(hits) == 1


# --------------------------------------------------------------------- #
# An empty contract promises nothing

def test_no_declared_patterns_means_no_violations_ever():
    """An empty must_not_change means "nothing was declared off-limits", which
    is different from "nothing may change" and must never be read as it."""
    c = C.make(goal="do the thing")
    assert C.violations(c, ["anything.py", "everything.py"], "/proj") == []


def test_an_entirely_empty_contract_renders_nothing():
    assert C.prompt_block(C.make()) == ""
    assert C.summary(C.make()) == ""
    assert C.make().is_empty()


def test_no_contract_at_all_is_survived():
    assert C.violations(None, ["a.py"], "/proj") == []
    assert C.prompt_block(None) == ""


# --------------------------------------------------------------------- #
# What it says

def test_the_prompt_block_carries_all_three_parts():
    c = C.make(goal="make the importer accept gzip",
               must_not_change=["tests"], check="pytest tests/test_import.py")
    block = C.prompt_block(c)
    assert "make the importer accept gzip" in block
    assert "tests" in block
    assert "pytest tests/test_import.py" in block
    assert "judge your own final report against these" in block.lower()


def test_the_block_tells_the_model_to_stop_rather_than_renegotiate_alone():
    block = C.prompt_block(C.make(must_not_change=["tests"]))
    assert "stop and say so" in block


def test_a_violation_note_never_claims_anything_was_reverted():
    """Undoing a file on the agent's own judgement is the revert_worker
    mistake, which threw away work nobody asked it to touch."""
    note = C.violation_note([{"path": "tests/test_a.py", "pattern": "tests"}])
    assert "Nothing has been reverted" in note
    assert "user's call" in note
    assert "tests/test_a.py" in note


def test_a_violation_note_is_empty_when_there_is_nothing_to_say():
    assert C.violation_note([]) == ""


# --------------------------------------------------------------------- #
# Round trip

def test_a_contract_survives_being_stored_and_read_back():
    c = C.make(goal="g", must_not_change=["tests", "*.lock"], check="pytest")
    back = C.Contract.from_dict(c.as_dict())
    assert back.goal == "g"
    assert back.must_not_change == ["tests", "*.lock"]
    assert back.check == "pytest"


def test_junk_from_disk_does_not_produce_a_broken_contract():
    assert C.Contract.from_dict(None).is_empty()
    assert C.Contract.from_dict({"must_not_change": "tests, src"}).must_not_change \
        == ["tests", "src"]


def test_the_lists_are_bounded():
    c = C.make(must_not_change=[f"p{i}" for i in range(100)])
    assert len(c.must_not_change) <= C.MAX_ITEMS


# --------------------------------------------------------------------- #
# The agent side

class _Events:
    def __init__(self):
        self.warns = []

    def warn(self, msg):
        self.warns.append(msg)

    def info(self, msg):
        pass


def _agent(wrote=(), contract=None):
    a = SimpleNamespace(
        workdir="/proj",
        messages=[],
        events=_Events(),
        contract=contract,
        _contract_reported=False,
        _turn_wrote_paths=set(wrote),
        rebuilt=0,
    )
    a.rebuild_system_prompt = lambda: None
    return a


def test_setting_a_contract_installs_it_immediately(tmp_path):
    """It lives in messages[0], so it has to go in now — not at the next
    natural rebuild, which may be after the work is done."""
    a = _agent()
    rebuilt = []
    a.rebuild_system_prompt = lambda: rebuilt.append(1)
    out = Agent._set_contract_tool(a, "make it accept gzip", ["tests"], "pytest")
    assert a.contract is not None
    assert a.contract.goal == "make it accept gzip"
    assert rebuilt, "the contract must reach the system prompt at once"
    assert "gzip" in out


def test_an_empty_contract_is_refused_rather_than_stored():
    a = _agent()
    with pytest.raises(ToolError):
        Agent._set_contract_tool(a, "", [], "")
    assert a.contract is None


def test_touching_a_forbidden_file_is_reported_to_both_the_user_and_the_model():
    a = _agent(wrote={"tests/test_a.py"}, contract=C.make(must_not_change=["tests"]))
    assert Agent._contract_breach_note(a) is True
    assert any("tests/test_a.py" in w for w in a.events.warns)
    (msg,) = a.messages
    assert "must not change" in msg["content"]
    assert "Nothing has been reverted" in msg["content"]


def test_nothing_is_said_when_the_contract_was_kept():
    a = _agent(wrote={"src/main.py"}, contract=C.make(must_not_change=["tests"]))
    assert Agent._contract_breach_note(a) is False
    assert a.messages == [] and a.events.warns == []


def test_it_reports_once_per_turn_not_every_step():
    a = _agent(wrote={"tests/test_a.py"}, contract=C.make(must_not_change=["tests"]))
    assert Agent._contract_breach_note(a) is True
    assert Agent._contract_breach_note(a) is False
    assert len(a.messages) == 1


def test_no_contract_means_no_check():
    a = _agent(wrote={"tests/test_a.py"}, contract=None)
    assert Agent._contract_breach_note(a) is False


def test_a_turn_that_wrote_nothing_is_not_checked():
    a = _agent(wrote=set(), contract=C.make(must_not_change=["tests"]))
    assert Agent._contract_breach_note(a) is False


def test_the_agent_never_reverts_on_its_own(monkeypatch):
    """The one behaviour this must never grow. A breach is a report."""
    a = _agent(wrote={"tests/test_a.py"}, contract=C.make(must_not_change=["tests"]))
    reverted = []
    a.backup_repo = SimpleNamespace(
        revert_paths_to=lambda *args, **kw: reverted.append(args))
    Agent._contract_breach_note(a)
    assert reverted == [], "a contract breach must never revert anything"
