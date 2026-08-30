"""Where is this codebase dangerous? — the rest of the history, read.

`why(path, line)` exists because the reasons are in git and the agent could
not reach them. It answers about ONE line. The rest of the log answers a
different question nobody was asking: which files bite, and what has to change
alongside what. A weak model cannot know that a file has been reverted twice
and rewritten six times, so it edits it with exactly the confidence it brings
to a file nobody has ever had trouble with.

Everything here comes from `git log` alone. No service, no model, no network,
and nothing is sent anywhere.

Four signals, and the reason each is here rather than the obvious version:

  - **Fix density, not churn.** A file with forty changes of which thirty say
    fix/revert/bug is a minefield; one with forty and none is merely active.
    Raw churn on its own just names the files somebody is working on this
    month.

  - **Revert proximity.** The strongest signal available and the one this
    repository was built to surface: a line that was tried and undone looks
    identical to a line nobody ever touched.

  - **Coupling by LIFT, not by co-occurrence.** This is the part that is easy
    to get wrong, and the repo's own history proves it. `CLAUDE.md` changes in
    42 of 51 commits here, so it co-occurs with *everything* at ~100% and a
    naive metric would report it as every file's closest partner — which is
    both true and useless. Lift divides by how often the sibling changes
    anyway, so `agent.py` (37% of all commits, 80% of the ones touching
    `prompts.py`) scores 2.1 and `CLAUDE.md` scores 1.2. The second number is
    the honest one.

  - **Bus factor.** One author, long untouched: whoever understood this may
    not be here, and the code has had no second reader.

The load-bearing rule is the same one as everywhere else in this app: **"I
could not find out" is not "there is nothing to worry about".** A repository
with no history at all is answered by `unavailable()` explicitly, rather than
by reporting every file as low risk — the reassuring, confident, wrong answer.

But that rule has a second half which the first version of this module got
wrong, and running it here is what showed it up: **partial history is not no
history.** This very checkout is shallow, and so is nearly every checkout an
agent works in — CI runners, cloud sessions, `--depth` clones. Refusing on a
shallow clone made the tool absent exactly where it mostly runs. So a
truncated log now produces its numbers WITH `caveat()` attached, saying every
count is a floor. Refusing to answer and answering as if certain are both
wrong; the third option is answering with the bound stated.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .tools import NO_WINDOW_KWARGS

# Below this a repository has not lived long enough for any of these numbers
# to mean anything, and saying so is better than ranking four commits.
MIN_COMMITS = 12

# How much history to read. Enough to be meaningful, bounded so this stays a
# sub-second operation on a large repository.
MAX_COMMITS = 800

# A commit that touches half the tree (a reformat, a licence header, a bulk
# rename) tells you nothing about which files belong together, and it drags
# every pair's co-occurrence up at once. Excluded from coupling only -- it is
# still a real change for churn.
MAX_FILES_FOR_COUPLING = 25

# Lift below this is noise: the sibling changes about as often as it would
# anyway. 1.0 means "no relationship at all", so the bar has to sit clear of it.
MIN_LIFT = 1.5
MIN_PAIR_COMMITS = 3

# SUBJECT only, and a deliberately tight list.
#
# The first version matched the commit BODY too, and this repository is what
# proved that wrong: its commit bodies are essays ABOUT failures -- "this was
# tried and reverted", "nothing said so", "the wrong answer" -- so nearly every
# file came back HIGH and the ranking was worthless. A body that discusses a
# revert is not a revert. What a commit DID is in its subject.
_FIX_WORDS = re.compile(
    r"\b(fix(e[sd])?|fixing|bug(fix)?|hotfix|regress(ion|ed)?|"
    r"broke(n)?|breakage|crash(ed|es)?)\b", re.I)

# git's own markers, not prose: `git revert` writes "Revert \"...\"" as the
# subject and "This reverts commit <sha>" into the body. Both are unambiguous
# in a way no word list can be.
_REVERT_SUBJECT = re.compile(r"^\s*revert\b", re.I)
_REVERT_TRAILER = re.compile(r"^\s*this reverts commit\b", re.I | re.M)


def _is_fix(commit: dict) -> bool:
    return bool(_FIX_WORDS.search(commit.get("subject") or ""))


def _is_revert(commit: dict) -> bool:
    return bool(_REVERT_SUBJECT.search(commit.get("subject") or "")
                or _REVERT_TRAILER.search(commit.get("body") or ""))

_RECORD = "\x1e"      # record separator -- cannot appear in a commit message
_FIELD = "\x1f"

_cache: dict = {}


def _git(argv: list, cwd: Path) -> str:
    """argv list, never a shell string.

    Same reason `why()` does it: these arguments carry paths that may hold
    spaces, and quoting them correctly for two different shells is a worse
    problem than using neither.
    """
    try:
        proc = subprocess.run(["git"] + argv, cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=30, **NO_WINDOW_KWARGS)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _head(cwd: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip()


def history(cwd=None, max_commits: int = MAX_COMMITS) -> list:
    """[{sha, subject, body, author, when, files}], newest first.

    Cached on the HEAD sha, so repeated calls inside one turn cost one
    subprocess and a new commit invalidates it by itself.
    """
    cwd = Path(cwd or Path.cwd())
    head = _head(cwd)
    if not head:
        return []
    key = (str(cwd.resolve()), head, max_commits)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    raw = _git(["log", f"-{max_commits}", "--no-merges",
                f"--format={_RECORD}%H{_FIELD}%an{_FIELD}%at{_FIELD}%s{_FIELD}%b{_FIELD}",
                "--name-only"], cwd)
    commits = []
    for chunk in raw.split(_RECORD):
        if not chunk.strip():
            continue
        parts = chunk.split(_FIELD)
        if len(parts) < 6:
            continue
        sha, author, when, subject, body = parts[:5]
        # The format string ends with a separator on purpose, so the --name-only
        # list is its own field. Without that trailing _FIELD the file names and
        # the commit body share one blob and have to be told apart by guessing
        # what "looks like a path" -- and this repository's commit bodies are
        # essays full of file names.
        files = [ln.strip() for ln in parts[5].splitlines() if ln.strip()]
        try:
            ts = float(when)
        except (TypeError, ValueError):
            ts = 0.0
        commits.append({"sha": sha, "author": author, "when": ts,
                        "subject": subject, "body": body, "files": files})
    _cache[key] = commits
    return commits


def unavailable(cwd=None) -> str:
    """Why these numbers cannot be produced AT ALL, or "" when they can.

    Returned rather than swallowed: a repository answered with "everything
    looks fine" when nothing was read is the reassuring, confident, wrong
    answer, and it is exactly the failure `_repo_state` was fixed for.

    A shallow clone is deliberately NOT in here -- see `caveat`.
    """
    cwd = Path(cwd or Path.cwd())
    if not _head(cwd):
        return ("not a git repository (or git is unavailable), so there is no "
                "history to read — this says nothing about the risk here")
    if len(history(cwd)) < MIN_COMMITS:
        return (f"only {len(history(cwd))} commit(s) of history, which is too few "
                f"for any of these numbers to mean anything")
    return ""


def is_shallow(cwd=None) -> bool:
    cwd = Path(cwd or Path.cwd())
    return _git(["rev-parse", "--is-shallow-repository"], cwd).strip() == "true"


def caveat(cwd=None) -> str:
    """A truth about the numbers that does not stop them being produced.

    The first version REFUSED on a shallow clone, and running it here is what
    showed that up: this checkout is shallow, and so is nearly every checkout
    an agent works in — CI runners, cloud sessions, `--depth` clones. A tool
    that answers "I cannot tell you" in the environment it mostly runs in is
    not cautious, it is absent.

    Partial history is not the same as no history. Every count from a
    truncated log is a FLOOR: "changed 3 times" may really be thirty. That is
    still worth knowing, as long as it is never rounded up into a claim about
    the whole history — so the numbers come out with this attached rather
    than not coming out.
    """
    cwd = Path(cwd or Path.cwd())
    if is_shallow(cwd):
        return (f"history is truncated (shallow clone, {len(history(cwd))} commits "
                f"read), so every count below is a FLOOR — the real number may be "
                f"much higher. `git fetch --unshallow` for the whole picture.")
    return ""


# Below this share of commits mentioning a fix in their subject, fix-density
# is not a signal here and ranking on it would be inventing one. Measured
# against this repository, where 3 of 52 subjects say "fix": its subjects are
# essay titles ("A request ends on the turn, not on a note about it"), which
# is a perfectly good convention and simply not one this can read.
MIN_FIX_SHARE = 0.08


def signal_quality(cwd=None) -> dict:
    """Which of these signals this repository can actually support.

    Said out loud rather than left as an empty result. A tool that quietly
    returns nothing is indistinguishable from a codebase with nothing wrong in
    it, and that confusion is the single most expensive habit this app has had.

    Coupling is message-INDEPENDENT -- it reads which paths appear in a commit,
    not what the commit says -- so it survives any convention. Fix density and
    reverts are message-dependent and do not.
    """
    cwd = Path(cwd or Path.cwd())
    commits = history(cwd)
    if not commits:
        return {"coupling": False, "fixes": False, "reverts": False,
                "note": "no history read"}
    fixes = sum(1 for c in commits if _is_fix(c))
    reverts = sum(1 for c in commits if _is_revert(c))
    share = fixes / len(commits)
    note = ""
    if share < MIN_FIX_SHARE:
        note = (f"only {fixes} of {len(commits)} commit subjects here name a fix, "
                f"so fix-density says nothing about this repository — its commit "
                f"subjects describe the change rather than classifying it. "
                f"Coupling below does not depend on commit messages and is "
                f"unaffected.")
    return {"coupling": len(commits) >= MIN_COMMITS,
            "fixes": share >= MIN_FIX_SHARE,
            "reverts": reverts > 0,
            "fix_commits": fixes, "revert_commits": reverts,
            "commits": len(commits), "note": note}


def _norm(path: str, cwd: Path) -> str:
    """A repo-relative posix path, from whatever the caller had."""
    p = str(path or "").replace("\\", "/").strip().lstrip("./")
    if not p:
        return ""
    try:
        abs_p = (cwd / p).resolve()
        rel = abs_p.relative_to(cwd.resolve())
        return rel.as_posix()
    except (OSError, ValueError):
        return p


def file_stats(path: str, cwd=None) -> dict:
    """Churn, fix density, reverts, authors and age for one file."""
    cwd = Path(cwd or Path.cwd())
    rel = _norm(path, cwd)
    commits = history(cwd)
    touching = [c for c in commits if rel in c["files"]]
    fixes = [c for c in touching if _is_fix(c)]
    reverts = [c for c in touching if _is_revert(c)]
    authors = {c["author"] for c in touching}
    last = max((c["when"] for c in touching), default=0.0)
    return {
        "path": rel,
        "changes": len(touching),
        "fixes": len(fixes),
        "reverts": len(reverts),
        "authors": sorted(authors),
        "last_change": last,
        "days_since": (time.time() - last) / 86400 if last else None,
        "fix_density": (len(fixes) / len(touching)) if touching else 0.0,
        "total_commits": len(commits),
    }


def coupled_with(path: str, cwd=None, limit: int = 5) -> list:
    """Files that change WITH this one, ranked by lift.

    Lift = P(sibling | this) / P(sibling). A value of 1.0 means the sibling
    changes exactly as often as it would anyway -- i.e. no relationship -- so
    a raw co-occurrence percentage would put the repository's busiest file at
    the top of every list and say nothing.
    """
    cwd = Path(cwd or Path.cwd())
    rel = _norm(path, cwd)
    commits = history(cwd)
    usable = [c for c in commits if len(c["files"]) <= MAX_FILES_FOR_COUPLING]
    total = len(usable)
    if total < MIN_COMMITS:
        return []
    touching = [c for c in usable if rel in c["files"]]
    if not touching:
        return []
    base = {}
    for c in usable:
        for f in c["files"]:
            base[f] = base.get(f, 0) + 1
    together = {}
    for c in touching:
        for f in c["files"]:
            if f != rel:
                together[f] = together.get(f, 0) + 1
    rows = []
    for f, n in together.items():
        if n < MIN_PAIR_COMMITS:
            continue
        p_given = n / len(touching)
        p_base = base.get(f, 0) / total
        lift = (p_given / p_base) if p_base else 0.0
        if lift < MIN_LIFT:
            continue
        rows.append({"path": f, "together": n, "of": len(touching),
                     "confidence": p_given, "lift": lift})
    # Filtered by lift, RANKED by confidence, and the two do different jobs.
    # Lift is what removes the file that changes with everything (CLAUDE.md
    # changes in 42 of 51 commits here, so it co-occurs with every file at
    # ~100% and means nothing). But the question a reader is actually asking
    # is "I changed X — how likely is it that Y needs changing too", and that
    # is confidence. Ranking by lift instead put a rarely-touched file with a
    # perfect record above `agent.py`, which follows `prompts.py` 79% of the
    # time and is the answer anybody wanted.
    rows.sort(key=lambda r: (-r["confidence"], -r["lift"]))
    return rows[:limit]


def forgotten_siblings(changed: list, cwd=None, limit: int = 4) -> list:
    """Files that usually change with the ones just changed, and did not.

    This is the whole feature in one function. This repository's own
    `UNTRUSTED_INPUT_RULE` gap -- the desktop prompt fixed, the phone left
    with no such rule at all, and nothing failing -- is precisely the shape it
    catches, and CLAUDE.md records that it was found by luck.

    Phrased as a frequency by the caller, never as a rule: it is a
    correlation, and stated as an instruction it would be obeyed where it is
    wrong.
    """
    cwd = Path(cwd or Path.cwd())
    changed_set = {_norm(p, cwd) for p in (changed or []) if p}
    changed_set.discard("")
    if not changed_set:
        return []
    out = {}
    for path in changed_set:
        for row in coupled_with(path, cwd, limit=limit):
            if row["path"] in changed_set:
                continue
            keep = out.get(row["path"])
            if keep is None or row["confidence"] > keep["confidence"]:
                out[row["path"]] = dict(row, because=path)
    rows = sorted(out.values(), key=lambda r: (-r["confidence"], -r["lift"]))
    return rows[:limit]


def _risk_score(stats: dict) -> tuple:
    """(label, why). A label, not a number out of ten: a score invites
    comparison it cannot support, and what the reader needs is whether to slow
    down."""
    changes, fixes, reverts = stats["changes"], stats["fixes"], stats["reverts"]
    if not changes:
        return "unknown", "no commits in the history read touch this file"
    if reverts:
        return "high", (f"{reverts} of its {changes} changes mention a revert — "
                        f"something here has been tried and undone before")
    if changes >= 8 and stats["fix_density"] >= 0.4:
        return "high", (f"{fixes} of {changes} changes were fixes "
                        f"({stats['fix_density']:.0%})")
    if changes >= 5 and stats["fix_density"] >= 0.25:
        return "medium", (f"{fixes} of {changes} changes were fixes "
                          f"({stats['fix_density']:.0%})")
    if len(stats["authors"]) == 1 and changes >= 5 and (stats["days_since"] or 0) > 180:
        return "medium", (f"one author, untouched for "
                          f"{int(stats['days_since'])} days")
    return "low", f"{fixes} of {changes} changes were fixes"


def describe(path: str, cwd=None) -> str:
    """The `risk` tool's output: what the history says about one file."""
    cwd = Path(cwd or Path.cwd())
    blocked = unavailable(cwd)
    rel = _norm(path, cwd)
    if blocked:
        return (f"Cannot assess {rel or path}: {blocked}.\n"
                f"Treat this as 'not known', not as 'safe'.")
    note = caveat(cwd)
    stats = file_stats(rel, cwd)
    if not stats["changes"]:
        return (f"{rel}: nothing in the last {stats['total_commits']} commits "
                f"touches this file. Either it is new, or it has been stable for "
                f"longer than the history read here — which are different things "
                f"and this cannot tell them apart.")
    quality = signal_quality(cwd)
    label, why = _risk_score(stats)
    if not quality["fixes"] and not stats["reverts"]:
        # The verdict came from a signal this repository cannot support. That
        # includes LOW, and especially LOW: "this file looks fine" derived from
        # a fix count that cannot be read is the reassuring, confident, wrong
        # answer this whole module exists to avoid. Keep the counts, drop the
        # verdict.
        label = "unknown"
        why = "fix history cannot be read in this repository — see the note below"
    lines = [f"{rel} — risk: {label.upper()} ({why})",
             f"  changed in {stats['changes']} of the last "
             f"{stats['total_commits']} commits"]
    if stats["reverts"]:
        lines.append(f"  {stats['reverts']} change(s) mention a revert — read `why` "
                     f"on the lines you are about to touch before changing them")
    if stats["days_since"] is not None:
        lines.append(f"  last changed {int(stats['days_since'])} day(s) ago by "
                     f"{', '.join(stats['authors'][:3])}")
    siblings = coupled_with(rel, cwd)
    if siblings:
        lines.append("  changes together with:")
        for row in siblings:
            lines.append(f"    {row['path']} — {row['together']} of "
                         f"{row['of']} times ({row['confidence']:.0%}, "
                         f"{row['lift']:.1f}x more often than it changes anyway)")
        lines.append("  Those are correlations from history, not rules. If your "
                     "change does not need them, say so and move on.")
    if quality.get("note"):
        lines.append(f"  NOTE: {quality['note']}")
    if note:
        lines.append(f"  NOTE: {note}")
    return "\n".join(lines)


def hotspots(cwd=None, limit: int = 10) -> list:
    """The riskiest files in the repository, for a panel or an overview."""
    cwd = Path(cwd or Path.cwd())
    if unavailable(cwd):
        return []
    commits = history(cwd)
    seen = {}
    for c in commits:
        for f in c["files"]:
            seen[f] = seen.get(f, 0) + 1
    rows = []
    for path, n in seen.items():
        if n < 3:
            continue
        stats = file_stats(path, cwd)
        label, why = _risk_score(stats)
        if label in ("low", "unknown"):
            continue
        rows.append({"path": path, "risk": label, "why": why,
                     "changes": stats["changes"], "fixes": stats["fixes"],
                     "reverts": stats["reverts"]})
    order = {"high": 0, "medium": 1}
    rows.sort(key=lambda r: (order.get(r["risk"], 2), -r["reverts"], -r["fixes"]))
    return rows[:limit]
