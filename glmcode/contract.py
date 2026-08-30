"""What the turn was actually for, written down where it survives.

The pipeline is prompt to diff, with nothing durable in between. Over a long
turn a weak model does something ADJACENT to what was asked, verifies that,
and reports DONE — honestly, by its own lights. `HONEST_REPORT_RULE` fixed the
reporting of that: it catches the model that knows it fell short. It cannot
catch the model that does not.

A contract is three short things agreed at the top of a task:

  - **what must become true**, in the user's own words;
  - **what must not change** — the files this task has no business touching;
  - **how we will know**, which is a command, or an explicit admission that
    there is no automated check and what was done instead.

Only the second is mechanically checkable, and that is deliberately where the
weight sits. "Did it achieve the goal" is a judgement, and this module does not
pretend to make it — it puts the goal somewhere the model and the reviewer can
both still see it after compaction. "Did it change something it promised not
to" is a set difference, and that one it answers.

The rules that keep this from being worse than nothing:

  - **A violation is REPORTED, never reverted.** Undoing a file on the agent's
    own judgement is the `revert_worker` mistake, which threw away work nobody
    asked it to touch. The user is told; they decide. Reverting is what the
    per-turn snapshots are for, and a human presses that.

  - **The model proposes; the conversation is the confirmation.** A contract
    invented silently and then graded against is the agent writing its own
    rubric and marking its own homework. It is set by a tool call, so it
    appears in the chat as an ordinary visible step and the user can say
    "no, not that" before any work happens.

  - **It lives beside the session, not in the message list.** A contract that
    is compacted away has evaporated on exactly the long turns it exists for.
    It goes into the system prompt, which `rebuild_system_prompt` owns and
    compaction never touches.

  - **`must_not_change` is patterns, not a promise about the whole tree.** An
    empty list means "nothing was declared off-limits", which is different
    from "nothing may change" and must never be read as the latter.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_GOAL = 400
MAX_ITEMS = 12
MAX_PATTERN = 120


@dataclass
class Contract:
    goal: str = ""
    must_not_change: list = field(default_factory=list)
    check: str = ""
    created: float = field(default_factory=time.time)

    def is_empty(self) -> bool:
        return not (self.goal or self.must_not_change or self.check)

    def as_dict(self) -> dict:
        return {"goal": self.goal, "must_not_change": list(self.must_not_change),
                "check": self.check, "created": self.created}

    @classmethod
    def from_dict(cls, data) -> "Contract":
        if not isinstance(data, dict):
            return cls()
        return cls(goal=str(data.get("goal") or "")[:MAX_GOAL],
                   must_not_change=_clean_patterns(data.get("must_not_change")),
                   check=str(data.get("check") or "")[:MAX_PATTERN],
                   created=float(data.get("created") or time.time()))


def _clean_patterns(items) -> list:
    if isinstance(items, str):
        items = [p for p in items.replace(",", "\n").splitlines()]
    if not isinstance(items, (list, tuple)):
        return []
    out = []
    for item in items:
        text = str(item or "").strip().replace("\\", "/").lstrip("./")
        if text and text not in out:
            out.append(text[:MAX_PATTERN])
        if len(out) >= MAX_ITEMS:
            break
    return out


def make(goal: str = "", must_not_change=None, check: str = "") -> Contract:
    return Contract(goal=str(goal or "").strip()[:MAX_GOAL],
                    must_not_change=_clean_patterns(must_not_change),
                    check=str(check or "").strip()[:MAX_PATTERN])


def _matches(rel: str, pattern: str) -> bool:
    """A pattern matches a path, a glob, or a whole directory prefix.

    Directory prefixes are handled explicitly because `fnmatch` does not:
    "glmcode/gui" would otherwise fail to match "glmcode/gui/app.py", and
    somebody writing a directory name and getting no protection from it is the
    worst possible way for this to be wrong -- it reads as covered.
    """
    if rel == pattern:
        return True
    if fnmatch.fnmatch(rel, pattern):
        return True
    prefix = pattern.rstrip("/") + "/"
    return rel.startswith(prefix)


def violations(contract: Contract, changed_paths, cwd=None) -> list:
    """Which declared-untouchable patterns the given paths hit.

    Returns [{"path": ..., "pattern": ...}]. An empty `must_not_change` yields
    nothing at all, and must not be read as "nothing may change".
    """
    if not contract or not contract.must_not_change:
        return []
    cwd = Path(cwd or Path.cwd())
    hits = []
    for raw in changed_paths or []:
        rel = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if not rel:
            continue
        try:
            rel = Path(rel)
            if rel.is_absolute():
                rel = rel.resolve().relative_to(cwd.resolve())
            rel = rel.as_posix()
        except (OSError, ValueError):
            rel = str(raw).replace("\\", "/")
        for pattern in contract.must_not_change:
            if _matches(rel, pattern):
                hits.append({"path": rel, "pattern": pattern})
                break
    return hits


def prompt_block(contract: Contract) -> str:
    """The contract as it appears in the system prompt, or "" if unset.

    In the SYSTEM prompt because it has to survive compaction -- a contract
    that evaporates has done so on exactly the long turns it exists for. The
    task itself still goes in the turn; this is the standing agreement about
    that task, which is a different thing and belongs in what the model IS.
    """
    if not contract or contract.is_empty():
        return ""
    lines = ["\n\n# What this task is for (agreed with the user)"]
    if contract.goal:
        lines.append(f"Must become true: {contract.goal}")
    if contract.must_not_change:
        lines.append("Must NOT change: " + ", ".join(contract.must_not_change)
                     + ". If you find you need to change one of these, stop and "
                       "say so rather than doing it — that is a change to the "
                       "agreement, and it is the user's to make.")
    if contract.check:
        lines.append(f"How we will know: {contract.check}")
    lines.append("Judge your own final report against these, not against how "
                 "much work you did. If part of it is not met, say which part.")
    return "\n".join(lines)


def violation_note(hits: list) -> str:
    """What the model is told when it has touched something it agreed not to.

    A report, not a refusal, and not a revert: undoing a file on the agent's
    own judgement is the revert_worker mistake, which threw away work nobody
    asked it to touch. It says what happened and asks for it to be surfaced,
    because the user is the one who decides.
    """
    if not hits:
        return ""
    lines = ["[Automatic check — not from the user] This turn changed files the "
             "contract for this task says must not change:"]
    for hit in hits[:MAX_ITEMS]:
        lines.append(f"  {hit['path']}  (matches \"{hit['pattern']}\")")
    lines.append(
        "Nothing has been reverted — that is the user's call, not yours. Either "
        "undo the part that was not needed, or explain in your final answer why "
        "the change was unavoidable. Do not quietly leave it unmentioned: an "
        "unrequested change discovered later is the expensive kind.")
    return "\n".join(lines)


def summary(contract: Contract) -> str:
    """One line for the UI and for a sub-agent's briefing."""
    if not contract or contract.is_empty():
        return ""
    parts = []
    if contract.goal:
        parts.append(contract.goal)
    if contract.must_not_change:
        parts.append("not touching " + ", ".join(contract.must_not_change[:3]))
    if contract.check:
        parts.append(f"verified by `{contract.check}`")
    return " — ".join(parts)
