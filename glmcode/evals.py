"""Does the scaffolding actually help? A way to find out instead of believing.

The README's central claim is that scaffolding is the biggest quality lever
available to a small free model, and the app is built on it: post-write syntax
checks, the project layout map, review_changes, the verify nudge, the green
loop, a fresh-eyes critic, refine passes, and parallel_attempts -- which runs
two or three isolated attempts and keeps the best.

Ninety-odd test files prove every one of those is WIRED UP. None of them
measures whether any of it makes the agent better. Each is a hypothesis that
has never been tested, and one of them spends three times the daily request
quota on a free tier metered in requests per day.

So: a fixture is a tiny repo, a goal, and a command that says whether the goal
was met. Score is binary and comes from the command, not from the model's
opinion of its own work. Nothing here talks to a provider by itself -- the
agent is injected, so the whole runner is exercised in CI against a scripted
model and only spends real quota when someone asks it to.

Two guards decide whether a number means anything, and both exist because
without them a suite can report 100% while measuring nothing:

  - The check is run BEFORE the agent. A case whose check already passes is
    reported as `invalid`, never as a pass. This is the same failure as a
    skipped test reading like a passing one, which this repo has been bitten
    by more than once.
  - Protected files are hashed before and after. "Make the tests pass" is
    trivially satisfied by deleting the tests, and an eval that rewards that
    is worse than no eval -- it actively selects for it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

CASE_FILE = "case.json"
FILES_DIR = "files"
DEFAULT_TIMEOUT = 300


# --------------------------------------------------------------------- #
# Cases

@dataclass
class Case:
    name: str
    task: str
    check: str
    root: Path
    timeout: int = DEFAULT_TIMEOUT
    # Files the agent must not rewrite to get a pass. Relative to the repo.
    protect: list = field(default_factory=list)

    @property
    def files(self) -> Path:
        return self.root / FILES_DIR


def load_case(directory) -> Case:
    directory = Path(directory)
    spec = json.loads((directory / CASE_FILE).read_text(encoding="utf-8"))
    missing = [k for k in ("task", "check") if not spec.get(k)]
    if missing:
        raise ValueError(f"{directory.name}: case.json is missing {', '.join(missing)}")
    if not (directory / FILES_DIR).is_dir():
        raise ValueError(f"{directory.name}: no {FILES_DIR}/ to copy")
    return Case(
        name=spec.get("name") or directory.name,
        task=spec["task"],
        check=spec["check"],
        root=directory,
        timeout=int(spec.get("timeout") or DEFAULT_TIMEOUT),
        protect=list(spec.get("protect") or []),
    )


def load_cases(root) -> list:
    root = Path(root)
    found = [load_case(d) for d in sorted(root.iterdir())
             if d.is_dir() and (d / CASE_FILE).is_file()]
    if not found:
        raise ValueError(f"no cases under {root} (each needs a {CASE_FILE})")
    return found


# --------------------------------------------------------------------- #
# What a run COSTS
#
# The runner could always measure quality. It could not measure price, and on
# the free tiers this app is built around that is the number that decides
# whether anyone dares press the button: Google's free tier gives Gemini 3.6
# Flash twenty requests a day, an agentic turn spends several, and a grid of
# four configurations over three cases is a day's allowance gone in one go --
# discovered, before this, as a 429 somewhere in the middle.
#
# Two halves, and the second is what makes the first honest: an ESTIMATE
# before the run, and the REAL number measured as it goes, so the estimate
# stops being a guess after the first suite anyone runs.

# Used only until there is real history to average. Deliberately a round,
# obviously-approximate number rather than a precise-looking one: an agentic
# turn's cost depends on the model, the case and the scaffolding, and a figure
# like 7.3 would imply a precision this cannot have.
REQUESTS_PER_CASE_GUESS = 8


def _requests_spent() -> int:
    """Total requests this machine has recorded today, across every model.

    Read from usage.py, which counts what WE sent -- the only number this app
    can know first-hand. It will over-count if another chat is running at the
    same time, and the budget says so rather than pretending to be exact.
    """
    try:
        from . import usage
        return sum(int(n) for n in usage.today().values())
    except Exception:
        return 0


def estimate_requests(cases: int, configs: int = 1, repeats: int = 1,
                      observed=None) -> int:
    """How many requests a run is about to cost.

    `observed` is the per-case request counts from previous runs; given any,
    their median replaces the guess. A number derived from this machine's own
    history beats a constant in this file, which cannot know the model.
    """
    per_case = REQUESTS_PER_CASE_GUESS
    real = [int(n) for n in (observed or []) if n and int(n) > 0]
    if real:
        per_case = max(1, int(statistics.median(real)))
    return max(0, int(cases) * max(1, int(configs)) * max(1, int(repeats)) * per_case)


@dataclass
class Budget:
    """A cap in requests, enforced BETWEEN cases.

    Between, not during: a case is a whole agentic turn and there is no
    sensible way to stop one half way that does not also throw away the
    requests already spent on it. So the cap is "do not START another case
    once this much has gone", and it is documented as such rather than
    presented as a hard ceiling it cannot actually be.
    """
    max_requests: int = 0            # 0 = uncapped
    # Through a lambda, not `default_factory=_requests_spent`: the latter binds
    # this module's function object when the class is created, so a test (or
    # anything else) that replaces the name later is silently ignored and the
    # baseline is read from the real usage file instead.
    started_at: int = field(default_factory=lambda: _requests_spent())

    def spent(self) -> int:
        return max(0, _requests_spent() - self.started_at)

    def remaining(self) -> int:
        if not self.max_requests:
            return -1                # uncapped
        return max(0, self.max_requests - self.spent())

    def exhausted(self) -> bool:
        return bool(self.max_requests) and self.spent() >= self.max_requests


class Journal:
    """A record of finished cells, so a suite that dies can be resumed.

    A grid search is the one thing here expensive enough that losing it
    matters: it is measured in a day's quota, and without this a run that
    failed at 80% left nothing behind and could not be retried until tomorrow.
    Written after every case rather than at the end, because the failure this
    exists for is the run not reaching the end.
    """

    def __init__(self, path):
        self.path = Path(path) if path else None
        self._done = {}
        if self.path and self.path.is_file():
            try:
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        self._done[row["key"]] = row
            except (OSError, json.JSONDecodeError, ValueError, KeyError):
                # A half-written last line is the normal way this file ends --
                # it is appended to during a run that was killed. Keep whatever
                # parsed and carry on; the unreadable tail is one case re-run,
                # which is the cheap failure here.
                pass

    def has(self, key: str) -> bool:
        return key in self._done

    def result_for(self, key: str):
        row = self._done.get(key)
        if not row:
            return None
        return Result(row["case"], row["status"], row.get("seconds", 0.0),
                      row.get("detail", ""), row.get("tool_calls", 0),
                      row.get("requests", 0))

    def record(self, key: str, result) -> None:
        self._done[key] = {"key": key, "case": result.case, "status": result.status,
                           "seconds": result.seconds, "detail": result.detail,
                           "tool_calls": result.tool_calls,
                           "requests": result.requests}
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(self._done[key]) + "\n")
        except OSError:
            pass                     # a lost journal costs a re-run, not the run

    def observed_requests(self) -> list:
        return [r.get("requests", 0) for r in self._done.values()]


# --------------------------------------------------------------------- #
# Running one

@dataclass
class Result:
    case: str
    status: str          # pass | fail | invalid | error
    seconds: float = 0.0
    detail: str = ""
    tool_calls: int = 0
    # Requests this case actually cost, measured from usage.py rather than
    # guessed. The free tiers this app is built around are metered in requests
    # per day, so this -- not seconds -- is the number that decides whether a
    # suite can be afforded, and it is the only one that can refine the
    # estimate shown before the next run.
    requests: int = 0

    def key(self, label: str, run: int = 0) -> str:
        """Identity of one cell of a grid, for the resume journal."""
        return f"{label}::{self.case}::{run}"

    @property
    def scored(self) -> bool:
        """Whether this result counts toward a rate at all. An invalid case
        measured nothing; averaging it in as a failure is just as wrong as
        averaging it in as a pass."""
        return self.status in ("pass", "fail")


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


def _run_check(command: str, workdir: Path, timeout: int) -> tuple:
    from . import tools
    previous = tools.get_workdir()
    tools.set_workdir(workdir)
    try:
        return tools.run_check_command(command, timeout_seconds=timeout)
    finally:
        tools.set_workdir(previous)


def run_case(case: Case, make_agent, workdir: Path) -> Result:
    """One case, in a throwaway copy of its files. `make_agent(path)` returns
    something with .run_turn({...}) and .messages -- the real Agent in anger, a
    scripted one in tests."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.files, workdir, dirs_exist_ok=True)

    # Before: the check must FAIL, or there is nothing for the agent to do and
    # a pass afterwards says nothing about the agent.
    code, output = _run_check(case.check, workdir, case.timeout)
    if code == 0:
        return Result(case.name, "invalid",
                      detail="the check already passed before the agent ran")

    before = {p: _digest(workdir / p) for p in case.protect}

    started = time.time()
    # Measured across the turn rather than counted inside the agent: every
    # request goes through api.py, which already records one, and a second
    # counter next to it would be a number that could disagree with the one
    # the rate-limit panel shows.
    requests_before = _requests_spent()
    try:
        agent = make_agent(workdir)
        agent.run_turn({"role": "user", "content": case.task})
    except Exception as e:
        return Result(case.name, "error", time.time() - started,
                      f"{type(e).__name__}: {e}",
                      requests=max(0, _requests_spent() - requests_before))
    seconds = time.time() - started
    spent = max(0, _requests_spent() - requests_before)
    calls = sum(len(m.get("tool_calls") or [])
                for m in getattr(agent, "messages", []) if isinstance(m, dict))

    # "Make the tests pass" is trivially satisfied by deleting the tests.
    changed = [p for p, d in before.items() if _digest(workdir / p) != d]
    if changed:
        return Result(case.name, "fail", seconds,
                      f"rewrote protected file(s): {', '.join(sorted(changed))}",
                      calls, spent)

    code, output = _run_check(case.check, workdir, case.timeout)
    if code == 0:
        return Result(case.name, "pass", seconds, "", calls, spent)
    return Result(case.name, "fail", seconds, output.strip()[-500:], calls, spent)


# --------------------------------------------------------------------- #
# Running the suite

@dataclass
class Report:
    label: str
    results: list

    @property
    def scored(self) -> list:
        return [r for r in self.results if r.scored]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def rate(self) -> float:
        """Pass rate over the cases that measured something. None-safe: an
        all-invalid suite has no rate, and reporting 0% for it would read as
        'the agent failed everything'."""
        n = len(self.scored)
        return (self.passed / n) if n else 0.0

    @property
    def unusable(self) -> list:
        return [r for r in self.results if not r.scored]

    def summary(self) -> str:
        lines = [f"{self.label}: {self.passed}/{len(self.scored)} "
                 f"({self.rate * 100:.0f}%)"]
        if self.scored:
            secs = statistics.median(r.seconds for r in self.scored)
            lines[0] += f"  median {secs:.0f}s"
        for r in self.results:
            mark = {"pass": "PASS", "fail": "FAIL",
                    "invalid": "SKIP", "error": "ERR "}[r.status]
            line = f"  {mark}  {r.case}"
            if r.detail:
                line += f"  — {r.detail.splitlines()[0][:90]}"
            lines.append(line)
        if self.unusable:
            lines.append(f"  ({len(self.unusable)} case(s) measured nothing — "
                         f"they are excluded from the rate, not counted as failures)")
        return "\n".join(lines)


def run_suite(cases, make_agent, tmp_root, label: str = "run",
              on_result=None, budget=None, journal=None, run: int = 0) -> Report:
    """`budget` stops before starting a case once the cap is spent; `journal`
    makes the whole thing resumable. Both default to off, so the signature
    every existing caller uses behaves exactly as it did."""
    tmp_root = Path(tmp_root)
    results = []
    for i, case in enumerate(cases):
        key = f"{label}::{case.name}::{run}"
        if journal is not None and journal.has(key):
            # Already done in an earlier attempt at this same run. Replayed
            # rather than re-run: re-running it would spend the quota the
            # journal exists to protect, and produce a second answer to a
            # question already answered.
            done = journal.result_for(key)
            results.append(done)
            if on_result:
                on_result(done)
            continue
        if budget is not None and budget.exhausted():
            # Reported as its own status, never as a failure: a case that was
            # not run measured nothing, and scoring it as a fail would say the
            # agent got it wrong when the truth is that we stopped asking.
            skipped = Result(case.name, "invalid",
                             detail=f"not run — request budget spent "
                                    f"({budget.spent()}/{budget.max_requests})")
            results.append(skipped)
            if on_result:
                on_result(skipped)
            continue
        workdir = tmp_root / f"{i:02d}-{case.name}"
        result = run_case(case, make_agent, workdir)
        results.append(result)
        if journal is not None:
            journal.record(key, result)
        if on_result:
            on_result(result)
    return Report(label, results)


def expand_grid(specs: list) -> list:
    """`["auto_fix_tests=false,true", "parallel_attempts=1,2"]` into the four
    override lists that is shorthand for.

    A grid is the shape the question actually has -- "which of these settings
    helps" is rarely about one flag -- and writing out the cartesian product by
    hand is how a run ends up missing the combination that mattered.
    """
    axes = []
    for spec in specs:
        field_name, _, values = str(spec).partition("=")
        field_name = field_name.strip()
        options = [v.strip() for v in values.split(",") if v.strip()]
        if not field_name or not options:
            raise ValueError(f"grid needs FIELD=value[,value...], got {spec!r}")
        axes.append([f"{field_name}={v}" for v in options])
    if not axes:
        return []
    combos = [[]]
    for axis in axes:
        combos = [combo + [option] for combo in combos for option in axis]
    return combos


def compare(reports) -> str:
    """Several configurations, side by side. This is the point of the whole
    module: a flag is worth keeping if its column is higher, and that is a
    question no test in this repo could previously answer."""
    lines = ["", "configuration            pass rate    median s   cases"]
    lines.append("-" * 56)
    for rep in reports:
        secs = (statistics.median(r.seconds for r in rep.scored)
                if rep.scored else 0.0)
        lines.append(f"{rep.label[:22]:<22}  {rep.rate * 100:>7.0f}%  "
                     f"{secs:>9.0f}  {len(rep.scored):>6}")
    return "\n".join(lines)


def best_of(reports) -> tuple:
    """(winner, baseline) from a set of reports.

    The baseline is the FIRST report -- the CLI always puts the unmodified
    configuration first -- because a winner with nothing to beat is not a
    result. Ties go to the baseline: a configuration that merely matched the
    defaults has not earned the right to change anybody's settings, and
    preferring the fancier one on a tie is how scaffolding accumulates
    without evidence.
    """
    scored = [r for r in reports if r.scored]
    if not scored:
        return None, None
    baseline = scored[0]
    winner = baseline
    for rep in scored[1:]:
        if rep.rate > winner.rate:
            winner = rep
    return winner, baseline


# --------------------------------------------------------------------- #
# CLI
#
# Separate from the logic above so the whole runner stays testable without a
# provider: everything here is argument parsing and building a real agent.

def _apply_overrides(cfg, pairs: list) -> str:
    """`--set parallel_attempts=2 --set thinking_mode=high`, applied to a Config.

    Types come from the existing attribute, so a typo'd name is refused rather
    than silently setting a field nobody reads -- which would show up as "the
    flag made no difference" and be believed."""
    applied = []
    for pair in pairs:
        key, _, raw = pair.partition("=")
        key, raw = key.strip(), raw.strip()
        if not hasattr(cfg, key):
            raise SystemExit(f"unknown config field: {key}")
        current = getattr(cfg, key)
        if isinstance(current, bool):
            value = raw.lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int):
            value = int(raw)
        elif isinstance(current, float):
            value = float(raw)
        else:
            value = raw
        setattr(cfg, key, value)
        applied.append(f"{key}={value}")
    return ", ".join(applied) or "defaults"


def build_agent(workdir: Path, overrides: list):
    from .agent import Agent
    from .api import ZaiClient
    from .ci import _CiEvents
    from .config import load_config
    cfg = load_config()
    cfg.mode = "yolo"          # a throwaway copy in a temp dir; nothing to guard
    _apply_overrides(cfg, overrides)
    client = ZaiClient(cfg.resolve_api_key(), cfg.base_url)
    return Agent(cfg, client, events=_CiEvents(), workdir=Path(workdir))


def main(argv=None) -> int:
    import argparse
    import tempfile

    ap = argparse.ArgumentParser(
        prog="python -m glmcode.evals",
        description="Measure whether the agent's scaffolding actually helps.")
    ap.add_argument("--cases", default="evals/cases", help="directory of case folders")
    ap.add_argument("--only", default="", help="run just this case by name")
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per configuration; a small model is not deterministic, "
                         "so one run of one case is an anecdote")
    ap.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                    help="config override, repeatable")
    ap.add_argument("--against", action="append", default=[], metavar="FIELD=VALUE",
                    help="a SECOND configuration to compare with, repeatable")
    ap.add_argument("--keep", default="", metavar="DIR",
                    help="keep the working copies here instead of a temp dir")
    ap.add_argument("--grid", action="append", default=[], metavar="FIELD=A,B",
                    help="try every combination of these values, repeatable")
    ap.add_argument("--budget", type=int, default=0, metavar="N",
                    help="stop before starting a case once N requests have gone "
                         "(0 = uncapped). The free tiers are metered per day, so "
                         "this is the cap that matters")
    ap.add_argument("--journal", default="", metavar="FILE",
                    help="record finished cases here so an interrupted run can be "
                         "resumed by passing the same file again")
    ap.add_argument("--save-profile", action="store_true",
                    help="store the winning configuration for this model, so new "
                         "chats use what was measured instead of what was guessed")
    ap.add_argument("--yes", action="store_true",
                    help="skip the cost confirmation")
    args = ap.parse_args(argv)

    from .config import load_config
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as e:
        print(f"Could not load cases: {e}")
        return 2
    if args.only:
        cases = [c for c in cases if c.name == args.only]
        if not cases:
            print(f"No case named {args.only!r}.")
            return 2

    if not load_config().resolve_api_key():
        print("No API key configured — these runs call a real model.")
        return 2

    # The baseline goes FIRST and is always present: best_of reads it as the
    # thing everything else has to beat, and a grid with no baseline can only
    # say which of several changes is least bad.
    configs = [(_label(args.set), args.set)]
    if args.against:
        configs.append((_label(args.against), args.against))
    for combo in expand_grid(args.grid):
        configs.append((_label(combo), args.set + combo))

    journal = Journal(args.journal) if args.journal else None
    estimate = estimate_requests(
        len(cases), len(configs), args.repeats,
        observed=journal.observed_requests() if journal else None)
    done = len(journal._done) if journal else 0
    print(f"{len(cases)} case(s) x {len(configs)} configuration(s) "
          f"x {args.repeats} repeat(s)")
    print(f"Roughly {estimate} model requests" +
          (f"; {done} already in the journal and will be replayed free." if done
           else ". The free tiers are metered per day -- see Settings for what is left."))
    if args.budget:
        print(f"Budget: stop before starting a case once {args.budget} have gone.")
    if not args.yes:
        try:
            if input("Run it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing run.")
                return 0
        except EOFError:
            print("Not a terminal and --yes was not passed; nothing run.")
            return 2
    budget = Budget(max_requests=args.budget) if args.budget else None

    root = Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="mnm-evals-"))
    reports = []
    try:
        for label, overrides in configs:
            for run in range(args.repeats):
                tag = label if args.repeats == 1 else f"{label} #{run + 1}"
                print(f"\n=== {tag} ===", flush=True)
                rep = run_suite(
                    cases, lambda w: build_agent(w, overrides),
                    root / _slugify(tag), label=tag,
                    on_result=lambda r: print(f"  {r.status:<7} {r.case}", flush=True),
                    budget=budget, journal=journal, run=run)
                print(rep.summary(), flush=True)
                reports.append(rep)
                if budget is not None and budget.exhausted():
                    print(f"\nBudget spent ({budget.spent()} requests). "
                          f"Re-run with the same --journal to carry on"
                          + (" tomorrow." if not args.journal else "."), flush=True)
        if len(reports) > 1:
            print(compare(reports))
        if args.save_profile:
            _save_profile(reports, configs, args.repeats, len(cases))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if all(r.rate > 0 for r in reports) else 1


def _save_profile(reports, configs, repeats: int, cases: int) -> None:
    """Store the winner for the model that was actually measured.

    Only ever called from the CLI, and only with --save-profile: a suite that
    silently rewrote how every future chat behaves would be the worst version
    of this feature, and the settings it writes are restricted to scaffolding
    knobs by evalprofile.PROFILE_FIELDS regardless.
    """
    from . import evalprofile
    from .config import load_config
    winner, baseline = best_of(reports)
    if winner is None:
        print("\nNothing measured, so nothing saved.")
        return
    cfg = load_config()
    settings = {}
    for label, overrides in configs:
        if label == winner.label:
            for pair in overrides:
                key, _, raw = pair.partition("=")
                settings[key.strip()] = raw.strip()
            break
    row = evalprofile.save(cfg.model, cfg.base_url,
                           evalprofile.sanitize(settings),
                           rate=winner.rate, baseline_rate=baseline.rate,
                           baseline_label=baseline.label, cases=cases,
                           repeats=repeats, label=winner.label)
    if winner.label == baseline.label:
        print(f"\nThe defaults won ({winner.rate:.0%}). Saved as the measured "
              f"profile for {cfg.model}, so this does not get re-asked.")
    else:
        print(f"\nSaved for {cfg.model}: {row['label']} "
              f"({winner.rate:.0%} vs {baseline.rate:.0%} for {baseline.label}). "
              f"New chats will use it and say so.")
    dropped = set(settings) - set(row["settings"])
    if dropped:
        # Said out loud rather than dropped quietly: somebody who ran a grid
        # over a non-scaffold field needs to know it was measured but not kept.
        print(f"Not stored (not a scaffold setting): {', '.join(sorted(dropped))}.")


def _label(overrides: list) -> str:
    return ", ".join(overrides) if overrides else "defaults"


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:60]


if __name__ == "__main__":
    raise SystemExit(main())
