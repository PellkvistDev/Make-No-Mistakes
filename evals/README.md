# Evals — does the scaffolding actually help?

The README's central claim is that scaffolding is the biggest quality lever
available to a small free model. The app is built on that claim: post-write
syntax checks, the project layout map, `review_changes`, the verify nudge, a
four-round green loop, a fresh-eyes critic, refine passes, and
`parallel_attempts`, which runs two or three isolated attempts and keeps the
best.

Ninety-odd test files prove every one of those is **wired up**. Until now
nothing measured whether any of it makes the agent **better** — so each is a
hypothesis, and one of them spends three times the daily request quota against
a free tier metered in requests per day.

## Running it

These call a real model and spend real quota, so they are never part of CI.

```sh
python -m glmcode.evals                       # every case, current settings
python -m glmcode.evals --only fix-off-by-one # just one
python -m glmcode.evals --repeats 3           # a small model is not deterministic
```

Comparing two configurations is the point:

```sh
python -m glmcode.evals --repeats 3 --against parallel_attempts=2
python -m glmcode.evals --repeats 3 --set thinking_mode=low --against thinking_mode=max
python -m glmcode.evals --repeats 3 --against verify_edits=true
```

`--set` takes any field on `Config`, typed from the field itself. A misspelled
name is refused rather than ignored — silently setting a field nobody reads
looks exactly like "the flag made no difference", which is the one conclusion
this tool must not produce by accident.

Add `--keep DIR` to leave the working copies behind and look at what the agent
actually did.

## Writing a case

A case is a folder:

```
evals/cases/<name>/
  case.json
  files/          copied into a temp dir; the agent works on the copy
```

```json
{
  "task": "The test suite fails. Fix the source so it passes. Don't change the tests.",
  "check": "python -m pytest -q",
  "protect": ["test_stats.py"],
  "timeout": 180
}
```

`check` decides the score, and nothing else does — not the model's own account
of its work.

## The two guards, and why a suite without them lies

**The check is run before the agent.** A case whose check already passes is
reported `invalid` and left out of the rate entirely — not counted as a pass,
and not counted as a failure either, because it measured nothing. This is the
same shape as a skipped test reading like a passing one, which this repo has
been bitten by more than once. `tests/test_evals.py` re-checks every shipped
case still starts out failing, so a fixture accidentally repaired elsewhere
cannot quietly become a free pass for every configuration.

**`protect` files are hashed before and after.** "Make the tests pass" is
trivially satisfied by deleting the tests. An eval that rewards that does not
merely fail to measure quality — it selects against it. Rewriting a protected
file is a `fail`, with the file named.

An agent that crashes is an `error`, not a `fail`: a run that never happened
says nothing about whether the model could have done it.

## The cases

| Case | What it needs |
|---|---|
| `fix-off-by-one` | Read a failing test, find a one-character bug in the source |
| `add-a-feature` | Write a new function to a spec expressed only as tests |
| `trace-across-files` | Follow a failure out of the test, through the caller, into a third file |

Three is a starting point, not a benchmark. The numbers get trustworthy as
cases are added — and a case is worth adding precisely when it is one the agent
sometimes fails.
