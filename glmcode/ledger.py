"""What goes wrong here: a record of the mistakes this model makes on this
project, so the next turn starts knowing them.

The app is named for mistakes and kept no memory of a single one it made. An
`edit_file` whose `old_string` never existed, a stale-sha 422, a command the
permission engine refused, a sub-agent that came back FAILED -- each was
answered into the model's context and then fell out of it at the next
compaction. Nothing accumulated. The same model made the same mistake on the
same repo on Tuesday that it made on Monday, and `remember` only ever held
what somebody thought to assert.

This is `remember`, earned instead of asserted. Failures funnel through one
place already (Agent._tool_reply), so the observation costs nothing; what
turns a pile of errors into something useful is the SIGNATURE -- the error
text with its paths, numbers and quoted strings replaced by placeholders, so
"old_string not found in foo.py" and the same in bar.py are one pattern with a
count of two rather than two patterns with a count of one each. A pattern with
a count is a rule; a log of errors is not.

Four things here are load-bearing.

  - **Per model AND per endpoint.** The fallback chain switches models
    mid-task, and the same model name on another provider is a different
    quota and, empirically, a different failure profile. A lesson learned
    from Flash Lite handed to GLM is noise in the longest prompt in the app.

  - **A success decays a pattern; it never deletes it.** A rule that vanishes
    the moment it stops firing destroys the evidence it was ever a problem --
    the same permanent-loss failure as the sync index, where an answer that
    could not be recovered was written over one that could. Confidence is
    failures/(failures+successes), so a tool that now works erodes its own
    old patterns and they stop being injected, while the record of what
    happened stays readable.

  - **The block is capped hard.** It competes with the ~12,400 tokens of
    system prompt and tool schemas re-sent on every request, and a prefix
    that grows every session is a prompt cache that never hits. Six rules and
    ~1,200 characters, whatever the ledger holds.

  - **Whether the model had already been WARNED is recorded.** This is the
    only way to find out if a rule works. A pattern that keeps firing while
    its own rule sits in the prompt is not a lesson the model failed to
    learn; it is a rule that does not work, and it says so in its own text
    rather than being repeated more loudly forever.

Nothing here raises. A bookkeeper that breaks a turn is worse than no
bookkeeper, which is the same bargain usage.py makes next door.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import CONFIG_DIR

LEDGER_FILE = CONFIG_DIR / "ledger.json"
VERSION = 1

# A pattern has to happen this many times before it is worth a line in the
# longest prompt in the app. Twice is a coincidence; three times is a habit.
MIN_HITS = 3

# failures / (failures + successes). Below this the tool is mostly working and
# the pattern is history rather than a live problem -- so it stops being
# injected. The record itself stays; see the module docstring.
MIN_CONFIDENCE = 0.25

# The cap. Both limits are enforced, the character one last, because a single
# very long error can otherwise blow the budget on its own.
MAX_RULES = 6
MAX_RULE_CHARS = 1200

# A memory bound, not a policy: every pattern is a dict held in a file read on
# every prompt build. Eviction takes the least-confident, least-recent first,
# and is the ONLY thing here that deletes -- a success never does.
MAX_PATTERNS_PER_BUCKET = 200

# One lock for the process. Sub-agents and workers run in threads and share
# the file, so without this a parallel spawn loses records to a
# read-modify-write race. Same bargain as usage.py.
_LOCK = threading.Lock()

# Order matters: quoted strings go first (they frequently CONTAIN paths and
# numbers, and once a path inside a quote has become <path> the quote no
# longer looks like the same quote), then the path shapes longest-first, then
# the scalars.
_QUOTED = re.compile(r"""(['"`])(?:\\.|(?!\1).)*\1""", re.S)
_WINPATH = re.compile(r"[A-Za-z]:[\\/][^\s'\"]+")
# The lookbehind matters: without it this matched the "/a.py" inside
# "src/a.py" and left "src" sitting in the signature, so the same
# mistake about two files stayed two patterns of one hit each -- which
# is precisely the counting this module exists to do.
_POSIXPATH = re.compile(r"(?<![\w])(?:\./|\.\./|/)[\w./\-]{2,}")
# A relative path with no leading marker -- "src/a.py", "lib/deep/b.py". Left
# out of the first version, which is how "not found in src/a.py" and the same
# error about "lib/deep/b.py" stayed two patterns of one hit instead of
# becoming one pattern of two: the absolute forms above matched only from the
# slash onwards and left the first segment sitting in the signature.
_RELPATH = re.compile(r"\b[\w.\-]+(?:[\\/][\w.\-]+)+")
_DOTTEDFILE = re.compile(
    r"\b[\w\-]+\.(?:py|js|mjs|ts|tsx|jsx|json|md|css|html|txt|yml|yaml|toml|"
    r"go|rs|java|rb|sh|ps1|cfg|ini|lock|sql|xml)\b", re.I)
_HEX = re.compile(r"\b[0-9a-f]{7,}\b", re.I)
_NUM = re.compile(r"\b\d+\b")
_WS = re.compile(r"\s+")

# The agent prefixes every failure with this before it reaches the model.
# Stripped so the signature is about what went wrong, not about who reported it.
_ERROR_PREFIX = re.compile(r"^\s*ERROR:\s*", re.I)

MAX_SIG_CHARS = 160
MAX_SAMPLE_CHARS = 220


def signature(tool: str, error: str) -> str:
    """A stable key for "this kind of failure", from one failure's text.

    Everything that varies between two instances of the same mistake is
    replaced: the file it happened to be about, the line number, the sha, the
    string that was not found. What is left is the shape, which is the thing
    worth counting.
    """
    text = _ERROR_PREFIX.sub("", (error or "").strip())
    text = _QUOTED.sub("<str>", text)
    text = _WINPATH.sub("<path>", text)
    text = _POSIXPATH.sub("<path>", text)
    text = _RELPATH.sub("<path>", text)
    text = _DOTTEDFILE.sub("<path>", text)
    text = _HEX.sub("<id>", text)
    text = _NUM.sub("<n>", text)
    text = _WS.sub(" ", text).strip().lower()
    return f"{(tool or '?').strip()}: {text[:MAX_SIG_CHARS]}"


def _endpoint_key(endpoint: str) -> str:
    """The host, not the whole URL. Two chats on the same provider differ in
    path (/v1 vs /v1beta/openai) often enough that keying on the full URL
    would split one provider's history in half for no reason."""
    raw = (endpoint or "").strip()
    if not raw:
        return "local"
    try:
        host = urlparse(raw).netloc
    except ValueError:
        host = ""
    return host or raw[:60]


def bucket_key(model: str, endpoint: str) -> str:
    return f"{(model or 'unknown').strip()} @ {_endpoint_key(endpoint)}"


def project_key(project) -> str:
    """Resolved absolute path. Readable on purpose -- this file is meant to be
    openable by the person whose mistakes are in it."""
    try:
        return str(Path(project).resolve())
    except (OSError, ValueError, TypeError):
        return str(project or "?")


# --------------------------------------------------------------------- #
# Storage

def _blank() -> dict:
    return {"version": VERSION, "projects": {}}


def _read() -> dict:
    try:
        data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return _blank()
    # An older/newer version is read for what it has rather than discarded:
    # the shape is additive and the worst case of a missing field is one
    # pattern that has to be relearned.
    return {"version": data.get("version", VERSION), "projects": projects}


def _write(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def _bucket(data: dict, project: str, bucket: str) -> dict:
    proj = data["projects"].setdefault(project, {})
    if not isinstance(proj, dict):
        proj = {}
        data["projects"][project] = proj
    b = proj.setdefault(bucket, {})
    if not isinstance(b, dict):
        b = {}
        proj[bucket] = b
    return b


def _confidence(row: dict) -> float:
    n = int(row.get("n", 0) or 0)
    ok = int(row.get("ok", 0) or 0)
    total = n + ok
    return (n / total) if total else 0.0


def _evict(b: dict) -> None:
    """Bound the bucket. Least confident first, oldest breaking the tie."""
    if len(b) <= MAX_PATTERNS_PER_BUCKET:
        return
    ranked = sorted(b.items(),
                    key=lambda kv: (_confidence(kv[1]),
                                    float(kv[1].get("last", 0) or 0)))
    for sig, _ in ranked[:len(b) - MAX_PATTERNS_PER_BUCKET]:
        b.pop(sig, None)


# --------------------------------------------------------------------- #
# Recording

def record_failure(project, model: str, endpoint: str, tool: str, error: str,
                   was_warned: bool = False) -> None:
    """One tool call failed. Never raises.

    `was_warned` is whether a rule for this exact signature was already in the
    system prompt when it happened -- the only evidence available about
    whether these rules do any good.
    """
    if not tool:
        return
    try:
        sig = signature(tool, error)
        now = time.time()
        with _LOCK:
            data = _read()
            b = _bucket(data, project_key(project), bucket_key(model, endpoint))
            row = b.get(sig)
            if not isinstance(row, dict):
                row = {"tool": tool, "n": 0, "ok": 0, "warned": 0,
                       "first": now, "sample": ""}
            row["tool"] = tool
            row["n"] = int(row.get("n", 0) or 0) + 1
            row["last"] = now
            if was_warned:
                row["warned"] = int(row.get("warned", 0) or 0) + 1
            # Keep the most recent real text, not the first. The wording of a
            # provider's error changes, and the useful sample is the one that
            # matches what the reader will see next.
            row["sample"] = _ERROR_PREFIX.sub("", (error or "").strip())[:MAX_SAMPLE_CHARS]
            b[sig] = row
            _evict(b)
            _write(data)
    except Exception:
        pass


def record_success(project, model: str, endpoint: str, tool: str) -> None:
    """One tool call worked. Decays every pattern recorded for that tool in
    this bucket -- a tool that now works erodes its own old lessons. Never
    deletes: see the module docstring. Never raises."""
    if not tool:
        return
    try:
        with _LOCK:
            data = _read()
            proj = data["projects"].get(project_key(project))
            if not isinstance(proj, dict):
                return
            b = proj.get(bucket_key(model, endpoint))
            if not isinstance(b, dict):
                return
            touched = False
            for row in b.values():
                if isinstance(row, dict) and row.get("tool") == tool:
                    row["ok"] = int(row.get("ok", 0) or 0) + 1
                    touched = True
            if touched:
                _write(data)
    except Exception:
        pass


# --------------------------------------------------------------------- #
# Reading

def _live_rows(project, model: str, endpoint: str) -> list:
    """Patterns currently worth acting on, most-hit first. Never raises."""
    try:
        with _LOCK:
            data = _read()
        proj = data["projects"].get(project_key(project))
        if not isinstance(proj, dict):
            return []
        b = proj.get(bucket_key(model, endpoint))
        if not isinstance(b, dict):
            return []
        rows = []
        for sig, row in b.items():
            if not isinstance(row, dict):
                continue
            if int(row.get("n", 0) or 0) < MIN_HITS:
                continue
            if _confidence(row) < MIN_CONFIDENCE:
                continue
            rows.append((sig, row))
        rows.sort(key=lambda sr: (-int(sr[1].get("n", 0) or 0),
                                  -float(sr[1].get("last", 0) or 0)))
        return rows
    except Exception:
        return []


def active_signatures(project, model: str, endpoint: str) -> set:
    """The signatures whose rules are in the prompt right now.

    Agent asks this before recording a failure, so `was_warned` is the truth
    about what the model had actually been told rather than a guess. Kept in
    step with rules_block by both using _live_rows and the same cap.
    """
    return {sig for sig, _ in _live_rows(project, model, endpoint)[:MAX_RULES]}


def _rule_line(sig: str, row: dict) -> str:
    tool = row.get("tool") or "?"
    n = int(row.get("n", 0) or 0)
    warned = int(row.get("warned", 0) or 0)
    sample = (row.get("sample") or sig).strip()
    line = f"- `{tool}` has failed this way {n} times here: {sample}"
    if warned >= 2:
        # Said plainly rather than by repeating the rule harder. A pattern
        # that keeps firing with its own warning already in front of the model
        # is a warning that does not work, and the reader (a human, reading
        # the ledger panel) is the one who can act on that.
        line += (f" — and {warned} of those happened while this very warning "
                 f"was in front of you, so re-reading it is not the fix: "
                 f"change the approach.")
    return line


def rules_block(project, model: str, endpoint: str) -> str:
    """The learned-rules section for the system prompt, or "" if nothing has
    been learned yet. Capped by MAX_RULES and then by MAX_RULE_CHARS."""
    rows = _live_rows(project, model, endpoint)[:MAX_RULES]
    if not rows:
        return ""
    lines, used = [], 0
    for sig, row in rows:
        line = _rule_line(sig, row)
        if used + len(line) > MAX_RULE_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return (
        "\n\n# Mistakes you have actually made here\n"
        "Recorded from this project's own history with this exact model — not "
        "advice, a count. Each line is a tool call that really did fail this "
        "way, more than once. Avoid repeating them; if you are about to make "
        "one of these calls, do the cheap check that would catch it first "
        "(re-read the exact region before editing it, check a path exists "
        "before writing to it, read a command's help before guessing a flag).\n"
        + "\n".join(lines)
    )


def advice_for(project, model: str, endpoint: str, tool: str, error: str) -> str:
    """A sentence to append to a failure the model is about to read, when that
    failure is one it has made here before.

    The ledger does not know the fix, and says so rather than inventing one:
    what it knows first-hand is the count, and that repeating the call has not
    worked any of the previous times. Returns "" for a first-time failure --
    the ordinary case, where there is nothing to add.
    """
    if not tool:
        return ""
    try:
        sig = signature(tool, error)
        with _LOCK:
            data = _read()
        proj = data["projects"].get(project_key(project))
        if not isinstance(proj, dict):
            return ""
        b = proj.get(bucket_key(model, endpoint))
        if not isinstance(b, dict):
            return ""
        row = b.get(sig)
        if not isinstance(row, dict):
            return ""
        # The count INCLUDING the failure being reported right now: record_failure
        # runs before this, so row["n"] already has it.
        n = int(row.get("n", 0) or 0)
        if n < 2:
            return ""
        return (f"\n\n[Ledger] You have hit this same failure {n} times in this "
                f"project with this model. Retrying it as-is has not worked any "
                f"of those times — change the approach rather than the wording.")
    except Exception:
        return ""


def report(project=None) -> list:
    """Everything recorded, for the "what goes wrong here" panel. Includes
    patterns below the injection thresholds -- the panel's job is to show what
    happened, not only what is currently loud enough to be a rule.

    Returns a list of dicts, most-hit first.
    """
    try:
        with _LOCK:
            data = _read()
        want = project_key(project) if project is not None else None
        out = []
        for proj, buckets in data["projects"].items():
            if want is not None and proj != want:
                continue
            if not isinstance(buckets, dict):
                continue
            for bucket, rows in buckets.items():
                if not isinstance(rows, dict):
                    continue
                for sig, row in rows.items():
                    if not isinstance(row, dict):
                        continue
                    out.append({
                        "project": proj,
                        "bucket": bucket,
                        "signature": sig,
                        "tool": row.get("tool") or "?",
                        "failures": int(row.get("n", 0) or 0),
                        "successes": int(row.get("ok", 0) or 0),
                        "warned_and_failed": int(row.get("warned", 0) or 0),
                        "confidence": round(_confidence(row), 3),
                        "active": (int(row.get("n", 0) or 0) >= MIN_HITS
                                   and _confidence(row) >= MIN_CONFIDENCE),
                        "last": float(row.get("last", 0) or 0),
                        "sample": row.get("sample") or "",
                    })
        out.sort(key=lambda r: (-r["failures"], -r["last"]))
        return out
    except Exception:
        return []


def forget(project=None) -> None:
    """Clear the ledger, all of it or one project's. The user's own call --
    nothing in the app does this on its own."""
    try:
        with _LOCK:
            if project is None:
                LEDGER_FILE.unlink(missing_ok=True)
                return
            data = _read()
            if data["projects"].pop(project_key(project), None) is not None:
                _write(data)
    except Exception:
        pass
