"""The eval harness: the thing that decides whether a number means anything.

Every scaffolding feature in this app is a hypothesis -- syntax checks, the
project map, review_changes, the verify nudge, the green loop, refine passes,
parallel_attempts. The suite proves they are wired up. Nothing measured whether
they help, and one of them spends three times the daily request quota.

This file tests the RUNNER, not the agent: the agent is injected, so everything
here runs offline against a scripted stand-in and the real thing only spends
quota when a person asks for it.

Most of these are about the two ways an eval lies to you:

  - a case whose check already passes, which scores 100% while measuring
    nothing (the same shape as a skipped test reading like a passing one);
  - a case satisfied by deleting the test, which does not merely fail to
    measure quality -- it selects against it.
"""

import json

import pytest

from glmcode import evals


# --------------------------------------------------------------- helpers --

def _write_case(root, name, *, task="do the thing", check, files, protect=None,
                timeout=30):
    d = root / name
    (d / evals.FILES_DIR).mkdir(parents=True)
    spec = {"task": task, "check": check, "timeout": timeout}
    if protect:
        spec["protect"] = protect
    (d / evals.CASE_FILE).write_text(json.dumps(spec), encoding="utf-8")
    for rel, body in files.items():
        (d / evals.FILES_DIR / rel).write_text(body, encoding="utf-8")
    return d


class _Agent:
    """A stand-in for the real one: it edits files instead of thinking."""

    def __init__(self, workdir, edits=None, boom=None, calls=1):
        self.workdir = workdir
        self.edits = edits or {}
        self.boom = boom
        self.messages = [{"role": "assistant",
                          "tool_calls": [{"id": str(i)} for i in range(calls)]}]

    def run_turn(self, _msg):
        if self.boom:
            raise self.boom
        for rel, body in self.edits.items():
            (self.workdir / rel).write_text(body, encoding="utf-8")


def _maker(**kw):
    return lambda workdir: _Agent(workdir, **kw)


PASSES = "import sys; sys.exit(0)"
FAILS = "import sys; sys.exit(1)"


def _check(script):
    return f'python -c "{script}"'


# ------------------------------------------------------------- loading ----

def test_a_case_is_a_folder_with_a_spec_and_some_files(tmp_path):
    _write_case(tmp_path, "one", check=_check(FAILS), files={"a.py": "x = 1\n"})
    (case,) = evals.load_cases(tmp_path)
    assert case.name == "one"
    assert (case.files / "a.py").is_file()


def test_a_spec_missing_the_task_or_the_check_is_refused(tmp_path):
    d = tmp_path / "broken"
    (d / evals.FILES_DIR).mkdir(parents=True)
    (d / evals.CASE_FILE).write_text('{"task": "do it"}', encoding="utf-8")
    with pytest.raises(ValueError, match="check"):
        evals.load_case(d)


def test_an_empty_directory_is_an_error_not_an_empty_run(tmp_path):
    """A suite that silently finds no cases reports a clean sweep of nothing."""
    with pytest.raises(ValueError, match="no cases"):
        evals.load_cases(tmp_path)


# ------------------------------------------------- the two lying failures --

def test_a_case_that_already_passes_is_invalid_not_a_pass(tmp_path):
    """The guard the whole harness rests on. If the check passes before the
    agent runs, the agent had nothing to do and a pass afterwards says nothing
    about it -- so it is never reported as one."""
    _write_case(tmp_path, "already-green", check=_check(PASSES),
                files={"a.py": "x = 1\n"})
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(), tmp_path / "work")

    assert result.status == "invalid"
    assert "already passed" in result.detail


def test_an_invalid_case_is_left_out_of_the_rate_entirely(tmp_path):
    """Counting it as a failure is as wrong as counting it as a pass: both
    report a measurement that was never taken."""
    _write_case(tmp_path, "real", check=_check(FAILS), files={"a.py": ""})
    _write_case(tmp_path, "already-green", check=_check(PASSES), files={"a.py": ""})
    cases = evals.load_cases(tmp_path)

    report = evals.run_suite(cases, _maker(), tmp_path / "w", label="t")

    assert len(report.scored) == 1
    assert len(report.unusable) == 1
    assert "measured nothing" in report.summary()


def test_deleting_the_test_to_go_green_is_a_failure(tmp_path):
    """"Make the tests pass" is trivially satisfied by removing them. An eval
    that rewards that actively selects for it."""
    _write_case(
        tmp_path, "cheatable",
        check='python -c "import sys, pathlib; '
              'sys.exit(0 if pathlib.Path(\'t.py\').read_text().strip() == \'ok\' else 1)"',
        files={"t.py": "not ok\n", "src.py": "\n"},
        protect=["t.py"])
    (case,) = evals.load_cases(tmp_path)

    # An "agent" that rewrites the protected file to make the check pass.
    result = evals.run_case(case, _maker(edits={"t.py": "ok\n"}), tmp_path / "w")

    assert result.status == "fail"
    assert "protected" in result.detail
    assert "t.py" in result.detail


def test_touching_an_unprotected_file_is_fine(tmp_path):
    """The guard must not forbid work -- only the shortcut."""
    _write_case(
        tmp_path, "honest",
        check='python -c "import sys, pathlib; '
              'sys.exit(0 if \'done\' in pathlib.Path(\'src.py\').read_text() else 1)"',
        files={"t.py": "keep me\n", "src.py": "\n"},
        protect=["t.py"])
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(edits={"src.py": "done\n"}), tmp_path / "w")

    assert result.status == "pass"


# --------------------------------------------------------- ordinary runs --

def test_a_fixed_case_passes(tmp_path):
    _write_case(
        tmp_path, "fixable",
        check='python -c "import sys, pathlib; '
              'sys.exit(0 if \'fixed\' in pathlib.Path(\'a.py\').read_text() else 1)"',
        files={"a.py": "broken\n"})
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(edits={"a.py": "fixed\n"}), tmp_path / "w")

    assert result.status == "pass"
    assert result.seconds >= 0


def test_an_untouched_case_fails_and_keeps_the_output(tmp_path):
    """The check's own output is what tells you WHY, and it is the only
    explanation a binary score comes with."""
    _write_case(tmp_path, "untouched",
                check='python -c "print(\'assert 1 == 2\'); raise SystemExit(1)"',
                files={"a.py": "broken\n"})
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(), tmp_path / "w")

    assert result.status == "fail"
    assert "assert 1 == 2" in result.detail


def test_an_agent_that_throws_is_an_error_not_a_failure(tmp_path):
    """A crashed run measured nothing about the agent's ability, so it must not
    be averaged in as though the model got the answer wrong."""
    _write_case(tmp_path, "crashy", check=_check(FAILS), files={"a.py": ""})
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(boom=RuntimeError("no key")), tmp_path / "w")

    assert result.status == "error"
    assert "no key" in result.detail
    assert result.scored is False


def test_the_working_copy_is_a_copy(tmp_path):
    """Cases are run over and over across configurations; one that edits its
    own fixtures would change the question between runs."""
    _write_case(tmp_path, "c", check=_check(FAILS), files={"a.py": "original\n"})
    (case,) = evals.load_cases(tmp_path)

    evals.run_case(case, _maker(edits={"a.py": "rewritten\n"}), tmp_path / "w")

    assert (case.files / "a.py").read_text(encoding="utf-8") == "original\n"


def test_tool_calls_are_counted(tmp_path):
    """Cost per solve, not just whether it solved it -- a config that passes
    the same cases with twice the calls is not the same result."""
    _write_case(
        tmp_path, "c",
        check='python -c "import sys, pathlib; '
              'sys.exit(0 if \'ok\' in pathlib.Path(\'a.py\').read_text() else 1)"',
        files={"a.py": "no\n"})
    (case,) = evals.load_cases(tmp_path)

    result = evals.run_case(case, _maker(edits={"a.py": "ok\n"}, calls=7),
                            tmp_path / "w")

    assert result.tool_calls == 7


# ---------------------------------------------------------- the numbers ---

def test_the_rate_covers_only_what_was_measured(tmp_path):
    rep = evals.Report("x", [
        evals.Result("a", "pass"), evals.Result("b", "fail"),
        evals.Result("c", "invalid"), evals.Result("d", "error"),
    ])
    assert rep.rate == 0.5
    assert rep.passed == 1
    assert len(rep.unusable) == 2


def test_a_suite_that_measured_nothing_does_not_report_a_score_of_zero():
    """0% reads as "the agent failed everything". It didn't -- it was never
    asked anything."""
    rep = evals.Report("x", [evals.Result("a", "invalid")])
    assert rep.scored == []
    assert rep.rate == 0.0
    assert "0/0" in rep.summary()


def test_configurations_are_shown_side_by_side():
    a = evals.Report("defaults", [evals.Result("c", "fail", 10.0)])
    b = evals.Report("parallel_attempts=2", [evals.Result("c", "pass", 20.0)])
    table = evals.compare([a, b])
    assert "defaults" in table and "parallel_attempts=2" in table
    assert "100%" in table and "0%" in table


# ------------------------------------------------------- config overrides --

def test_an_override_is_applied_with_the_field_s_own_type():
    from glmcode.config import Config
    cfg = Config()
    evals._apply_overrides(cfg, ["parallel_attempts=3", "thinking_mode=high"])
    assert cfg.parallel_attempts == 3 and isinstance(cfg.parallel_attempts, int)
    assert cfg.thinking_mode == "high"


def test_a_misspelled_field_is_refused_rather_than_ignored():
    """Silently setting a field nobody reads shows up as "the flag made no
    difference", which is exactly the conclusion this tool exists to produce
    honestly."""
    from glmcode.config import Config
    with pytest.raises(SystemExit, match="unknown config field"):
        evals._apply_overrides(Config(), ["parralel_attempts=2"])


def test_a_boolean_override_reads_words_not_just_digits():
    from glmcode.config import Config
    cfg = Config()
    evals._apply_overrides(cfg, ["verify_edits=true"])
    assert cfg.verify_edits is True


# ------------------------------------------------- the shipped fixtures ---

def test_the_shipped_cases_load():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "evals" / "cases"
    if not root.is_dir():
        pytest.skip("no shipped cases")
    cases = evals.load_cases(root)
    assert len(cases) >= 3
    for c in cases:
        assert c.task and c.check
        assert c.files.is_dir() and any(c.files.iterdir())


def test_every_shipped_case_starts_out_failing(tmp_path):
    """The property that makes them measurements. Checked here rather than
    trusted, because a fixture quietly repaired by an edit somewhere else would
    turn into a free pass for every configuration -- and a suite of free passes
    is indistinguishable from a suite the agent aced."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "evals" / "cases"
    if not root.is_dir():
        pytest.skip("no shipped cases")
    for case in evals.load_cases(root):
        # An agent that does nothing at all must never come out as a pass.
        result = evals.run_case(case, _maker(), tmp_path / case.name)
        assert result.status in ("fail", "error"), \
            f"{case.name} did not start out failing: {result.status} {result.detail}"
