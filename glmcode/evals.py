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
# Running one

@dataclass
class Result:
    case: str
    status: str          # pass | fail | invalid | error
    seconds: float = 0.0
    detail: str = ""
    tool_calls: int = 0

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
    try:
        agent = make_agent(workdir)
        agent.run_turn({"role": "user", "content": case.task})
    except Exception as e:
        return Result(case.name, "error", time.time() - started,
                      f"{type(e).__name__}: {e}")
    seconds = time.time() - started
    calls = sum(len(m.get("tool_calls") or [])
                for m in getattr(agent, "messages", []) if isinstance(m, dict))

    # "Make the tests pass" is trivially satisfied by deleting the tests.
    changed = [p for p, d in before.items() if _digest(workdir / p) != d]
    if changed:
        return Result(case.name, "fail", seconds,
                      f"rewrote protected file(s): {', '.join(sorted(changed))}", calls)

    code, output = _run_check(case.check, workdir, case.timeout)
    if code == 0:
        return Result(case.name, "pass", seconds, "", calls)
    return Result(case.name, "fail", seconds, output.strip()[-500:], calls)


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
              on_result=None) -> Report:
    tmp_root = Path(tmp_root)
    results = []
    for i, case in enumerate(cases):
        workdir = tmp_root / f"{i:02d}-{case.name}"
        result = run_case(case, make_agent, workdir)
        results.append(result)
        if on_result:
            on_result(result)
    return Report(label, results)


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

    configs = [(_label(args.set), args.set)]
    if args.against:
        configs.append((_label(args.against), args.against))

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
                    on_result=lambda r: print(f"  {r.status:<7} {r.case}", flush=True))
                print(rep.summary(), flush=True)
                reports.append(rep)
        if len(reports) > 1:
            print(compare(reports))
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    return 0 if all(r.rate > 0 for r in reports) else 1


def _label(overrides: list) -> str:
    return ", ".join(overrides) if overrides else "defaults"


def _slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text)[:60]


if __name__ == "__main__":
    raise SystemExit(main())
