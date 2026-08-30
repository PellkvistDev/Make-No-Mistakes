"""Closing the eval loop: cost, resumability, and a measurement that lands.

`evals.py` could always answer "does this flag help" — a case runner, an A/B,
a compare() table. Nothing in the app ever read the answer, and nothing
anywhere said what a run would COST on a tier metered at twenty requests a
day. These are the two halves that were missing, plus the guard that keeps a
measurement from turning into a config-editing machine.
"""

import json
from types import SimpleNamespace

import pytest

from glmcode import evalprofile, evals


# --------------------------------------------------------------------- #
# What a run costs

def test_the_estimate_scales_with_every_axis_of_the_grid():
    one = evals.estimate_requests(cases=3, configs=1, repeats=1)
    assert one == 3 * evals.REQUESTS_PER_CASE_GUESS
    assert evals.estimate_requests(3, 2, 1) == 2 * one
    assert evals.estimate_requests(3, 1, 2) == 2 * one


def test_real_history_replaces_the_guess():
    """A constant in this file cannot know the model. The moment there are
    real per-case numbers, they win."""
    est = evals.estimate_requests(cases=2, observed=[20, 20, 20])
    assert est == 40
    assert est != 2 * evals.REQUESTS_PER_CASE_GUESS


def test_zero_and_junk_observations_fall_back_rather_than_claiming_free():
    assert evals.estimate_requests(2, observed=[0, 0]) == 2 * evals.REQUESTS_PER_CASE_GUESS
    assert evals.estimate_requests(2, observed=[]) == 2 * evals.REQUESTS_PER_CASE_GUESS


def test_a_budget_measures_against_where_it_started(monkeypatch):
    spent = {"n": 100}
    monkeypatch.setattr(evals, "_requests_spent", lambda: spent["n"])
    b = evals.Budget(max_requests=10)
    assert b.spent() == 0 and not b.exhausted()
    spent["n"] = 105
    assert b.spent() == 5 and b.remaining() == 5 and not b.exhausted()
    spent["n"] = 110
    assert b.exhausted()


def test_no_cap_means_no_cap(monkeypatch):
    monkeypatch.setattr(evals, "_requests_spent", lambda: 10_000)
    b = evals.Budget(max_requests=0)
    assert not b.exhausted()
    assert b.remaining() == -1


# --------------------------------------------------------------------- #
# Running a suite under a budget / journal

def _case(tmp_path, name="c1", check="exit 1"):
    d = tmp_path / "cases" / name
    (d / evals.FILES_DIR).mkdir(parents=True)
    (d / evals.FILES_DIR / "a.txt").write_text("x", encoding="utf-8")
    (d / evals.CASE_FILE).write_text(
        json.dumps({"task": "do it", "check": check}), encoding="utf-8")
    return evals.load_case(d)


class _Agent:
    """Scripted: writes the file that makes the check pass, if asked to."""

    def __init__(self, workdir, succeed=True):
        self.workdir = workdir
        self.succeed = succeed
        self.messages = []

    def run_turn(self, msg):
        if self.succeed:
            (self.workdir / "done.txt").write_text("ok", encoding="utf-8")


def _maker(succeed=True):
    return lambda w: _Agent(w, succeed)


CHECK = "python -c \"import os,sys; sys.exit(0 if os.path.exists('done.txt') else 1)\""


def test_a_case_records_what_it_actually_cost(tmp_path, monkeypatch):
    spent = {"n": 0}

    def bump():
        return spent["n"]
    monkeypatch.setattr(evals, "_requests_spent", bump)

    case = _case(tmp_path, check=CHECK)

    def maker(w):
        spent["n"] += 7          # the "turn" costs seven requests
        return _Agent(w)
    r = evals.run_case(case, maker, tmp_path / "w")
    assert r.status == "pass"
    assert r.requests == 7


def test_an_exhausted_budget_stops_starting_cases_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "_requests_spent", lambda: 500)
    case = _case(tmp_path, check=CHECK)
    budget = evals.Budget(max_requests=1)
    budget.started_at = 0                       # already over
    rep = evals.run_suite([case], _maker(), tmp_path / "w", label="x", budget=budget)
    (r,) = rep.results
    assert r.status == "invalid"
    assert "budget" in r.detail.lower()


def test_a_case_not_run_is_never_scored_as_a_failure(tmp_path, monkeypatch):
    """The agent did not get it wrong; we stopped asking. Scoring it as a fail
    would report the model as worse than it is, on the one run that cost the
    most to produce."""
    monkeypatch.setattr(evals, "_requests_spent", lambda: 500)
    budget = evals.Budget(max_requests=1)
    budget.started_at = 0
    rep = evals.run_suite([_case(tmp_path, check=CHECK)], _maker(),
                          tmp_path / "w", label="x", budget=budget)
    assert rep.scored == []
    assert len(rep.unusable) == 1


def test_a_journal_replays_instead_of_respending(tmp_path):
    case = _case(tmp_path, check=CHECK)
    j = evals.Journal(tmp_path / "j.jsonl")
    rep1 = evals.run_suite([case], _maker(), tmp_path / "w1", label="run", journal=j)
    assert rep1.passed == 1

    calls = []

    def exploding(w):
        calls.append(w)
        raise AssertionError("must not run again")

    j2 = evals.Journal(tmp_path / "j.jsonl")
    rep2 = evals.run_suite([case], exploding, tmp_path / "w2", label="run", journal=j2)
    assert rep2.passed == 1
    assert calls == [], "a case already in the journal must not be re-run"


def test_a_half_written_journal_line_costs_one_case_not_the_run(tmp_path):
    """The normal way this file ends is mid-line, because it is appended to
    during a run that was killed."""
    p = tmp_path / "j.jsonl"
    p.write_text(json.dumps({"key": "run::c1::0", "case": "c1", "status": "pass",
                             "requests": 3}) + "\n{\"key\": \"run::c2",
                 encoding="utf-8")
    j = evals.Journal(p)
    assert j.has("run::c1::0")
    assert not j.has("run::c2::0")


# --------------------------------------------------------------------- #
# The grid

def test_a_grid_expands_to_every_combination():
    combos = evals.expand_grid(["auto_fix_tests=false,true", "parallel_attempts=1,2"])
    assert len(combos) == 4
    assert ["auto_fix_tests=false", "parallel_attempts=1"] in combos
    assert ["auto_fix_tests=true", "parallel_attempts=2"] in combos


def test_a_malformed_grid_axis_is_refused_rather_than_silently_dropped():
    with pytest.raises(ValueError):
        evals.expand_grid(["auto_fix_tests"])
    with pytest.raises(ValueError):
        evals.expand_grid(["=1,2"])


# --------------------------------------------------------------------- #
# Picking a winner

def _rep(label, statuses):
    return evals.Report(label, [evals.Result(f"c{i}", st)
                                for i, st in enumerate(statuses)])


def test_the_first_report_is_the_baseline_and_a_tie_goes_to_it():
    """A configuration that merely matched the defaults has not earned the
    right to change anybody's settings."""
    base = _rep("defaults", ["pass", "fail"])
    other = _rep("auto_fix_tests=true", ["pass", "fail"])
    winner, baseline = evals.best_of([base, other])
    assert baseline is base
    assert winner is base


def test_a_real_improvement_wins():
    base = _rep("defaults", ["fail", "fail"])
    other = _rep("auto_fix_tests=true", ["pass", "pass"])
    winner, baseline = evals.best_of([base, other])
    assert winner is other and baseline is base


def test_a_suite_that_measured_nothing_has_no_winner():
    winner, baseline = evals.best_of([_rep("defaults", ["invalid", "error"])])
    assert winner is None and baseline is None


# --------------------------------------------------------------------- #
# The profile: a measurement must not become a config-editing machine

@pytest.fixture
def profiles(monkeypatch, tmp_path):
    f = tmp_path / "profiles.json"
    monkeypatch.setattr(evalprofile, "PROFILE_FILE", f)
    monkeypatch.setattr(evalprofile, "CONFIG_DIR", tmp_path)
    return f


def test_only_scaffold_knobs_survive_sanitising():
    dirty = {"auto_fix_tests": True, "model": "something-else",
             "base_url": "https://evil", "api_key": "x", "parallel_attempts": 2}
    clean = evalprofile.sanitize(dirty)
    assert clean == {"auto_fix_tests": True, "parallel_attempts": 2}


def test_a_saved_profile_cannot_carry_a_model_or_a_key(profiles):
    evalprofile.save("m", "https://api.example.com/v1",
                     {"auto_fix_tests": True, "model": "hijacked", "api_key": "k"},
                     rate=0.9, baseline_rate=0.5, baseline_label="defaults", cases=3)
    row = evalprofile.get("m", "https://api.example.com/v1")
    assert row["settings"] == {"auto_fix_tests": True}
    assert "model" not in row["settings"] and "api_key" not in row["settings"]


def test_a_profile_is_keyed_on_the_model_and_the_endpoint(profiles):
    evalprofile.save("m", "https://a.example.com/v1", {"auto_fix_tests": True},
                     rate=0.9, baseline_rate=0.5, baseline_label="defaults", cases=3)
    assert evalprofile.get("m", "https://a.example.com/v1")
    assert evalprofile.get("m", "https://b.example.com/v1") is None
    assert evalprofile.get("other", "https://a.example.com/v1") is None


def test_nothing_measured_means_nothing_applied(profiles):
    cfg = SimpleNamespace(auto_fix_tests=False, parallel_attempts=1)
    assert evalprofile.apply_to(cfg, "m", "https://x/v1") == []
    assert cfg.auto_fix_tests is False


def test_applying_reports_only_what_it_actually_changed(profiles):
    evalprofile.save("m", "https://x/v1",
                     {"auto_fix_tests": True, "parallel_attempts": 1},
                     rate=0.9, baseline_rate=0.5, baseline_label="defaults", cases=3)
    cfg = SimpleNamespace(auto_fix_tests=False, parallel_attempts=1)
    changed = evalprofile.apply_to(cfg, "m", "https://x/v1")
    assert changed == ["auto_fix_tests=True"]
    assert cfg.auto_fix_tests is True
    # Applying twice is a no-op, so the caller stays silent rather than
    # announcing a change that did not happen.
    assert evalprofile.apply_to(cfg, "m", "https://x/v1") == []


def test_types_come_from_the_config_not_from_the_stored_string(profiles):
    """A profile that set a string where an int lives would be a setting
    nobody reads, showing up later as 'the flag made no difference'."""
    evalprofile.save("m", "https://x/v1", {"parallel_attempts": "3"},
                     rate=0.9, baseline_rate=0.5, baseline_label="defaults", cases=3)
    cfg = SimpleNamespace(parallel_attempts=1)
    evalprofile.apply_to(cfg, "m", "https://x/v1")
    assert cfg.parallel_attempts == 3
    assert isinstance(cfg.parallel_attempts, int)


def test_a_knob_this_build_no_longer_has_is_skipped_not_invented(profiles):
    evalprofile.save("m", "https://x/v1", {"auto_fix_tests": True},
                     rate=0.9, baseline_rate=0.5, baseline_label="defaults", cases=3)
    cfg = SimpleNamespace()                     # no such attribute
    assert evalprofile.apply_to(cfg, "m", "https://x/v1") == []
    assert not hasattr(cfg, "auto_fix_tests")


def test_the_defaults_winning_is_recorded_so_it_is_not_re_asked(profiles):
    """Stored with empty settings — distinguishable from 'nobody measured',
    which is the ordinary case and must stay tellable apart."""
    evalprofile.save("m", "https://x/v1", {}, rate=0.6, baseline_rate=0.6,
                     baseline_label="defaults", cases=3)
    row = evalprofile.get("m", "https://x/v1")
    assert row is not None and row["settings"] == {}


def test_describe_says_what_it_beat_and_by_how_much(profiles):
    evalprofile.save("m", "https://x/v1", {"auto_fix_tests": True},
                     rate=1.0, baseline_rate=0.5, baseline_label="defaults",
                     cases=4, label="auto_fix_tests=true")
    text = evalprofile.describe("m", "https://x/v1")
    assert "auto_fix_tests=true" in text
    assert "100%" in text and "50%" in text and "+50" in text


def test_an_unreadable_profile_file_is_survived(profiles):
    profiles.write_text("{ not json", encoding="utf-8")
    assert evalprofile.get("m", "https://x/v1") is None
    assert evalprofile.all_profiles() == {}
    cfg = SimpleNamespace(auto_fix_tests=False)
    assert evalprofile.apply_to(cfg, "m", "https://x/v1") == []


def test_forget_drops_one_without_touching_the_others(profiles):
    evalprofile.save("a", "https://x/v1", {"auto_fix_tests": True},
                     rate=0.9, baseline_rate=0.5, baseline_label="d", cases=1)
    evalprofile.save("b", "https://x/v1", {"auto_fix_tests": True},
                     rate=0.9, baseline_rate=0.5, baseline_label="d", cases=1)
    evalprofile.forget("a", "https://x/v1")
    assert evalprofile.get("a", "https://x/v1") is None
    assert evalprofile.get("b", "https://x/v1") is not None
