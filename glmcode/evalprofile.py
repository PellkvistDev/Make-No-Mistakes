"""The scaffold settings that were MEASURED to be best for a given model.

`glmcode/evals.py` could always answer "does this flag help" — it has a case
runner, an A/B and a `compare()` table. Nothing in the app had ever read its
answer. So the suite printed a number to a terminal, the number scrolled away,
and the settings stayed wherever they happened to be: every scaffolding
feature in this app is a hypothesis, and the instrument built to test them was
wired to nothing. This module is the wire.

A profile is one line of evidence: *for this model on this endpoint, this set
of scaffold settings beat this baseline, by this much, over this many cases.*
It is written by `python -m glmcode.evals --save-profile` and read when a chat
starts.

Load-bearing:

  - **Only scaffold knobs can be in it** (`PROFILE_FIELDS`). A profile is a
    measurement, and a measurement must not be able to change what it was not
    measuring — an eval run that could rewrite `model`, a base URL or a key
    would be a config-editing machine wearing a lab coat.

  - **Per model AND per endpoint**, the same key the mistake ledger uses and
    for the same reason: the same model name on another provider is a
    different quota and a different animal, and a profile measured on one
    would be applied to the other with nothing saying so.

  - **It is applied out loud, once per chat.** A silent reconfiguration is the
    worst version of this — the app quietly behaves differently from its own
    Settings screen and nothing anywhere explains why. Same rule the
    rate-limit fallback follows: news the first time, noise after that.

  - **Nothing exists until somebody measures.** There are no shipped defaults
    here and there must never be: a table of "good settings for Flash Lite"
    that nobody ran is exactly the guesswork this module exists to replace,
    and it would go stale the first time a provider changed a model.

  - **A profile records what it BEAT.** A winner with no baseline is not a
    result, and the difference is what says whether it was worth anything.
"""

from __future__ import annotations

import json
import threading
import time

from .config import CONFIG_DIR
from .ledger import bucket_key   # one key shape for "this model, this provider"

PROFILE_FILE = CONFIG_DIR / "scaffold_profiles.json"
VERSION = 1

# The only fields a measured profile may carry. Everything here is a
# scaffolding knob -- something the eval suite can actually vary and score.
# Adding a field to this set means claiming the suite can measure it.
PROFILE_FIELDS = frozenset({
    "verify_edits",
    "auto_fix_tests",
    "parallel_attempts",
    "thinking_mode",
    "codebase_memory_neural",
    "learn_from_mistakes",
})

_LOCK = threading.Lock()


def _blank() -> dict:
    return {"version": VERSION, "profiles": {}}


def _read() -> dict:
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return _blank()
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return _blank()
    return {"version": data.get("version", VERSION), "profiles": data["profiles"]}


def _write(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")


def sanitize(settings: dict) -> dict:
    """Drop anything that is not a scaffold knob. Silently, and deliberately
    so: the caller is a measurement runner, not a person, and the alternative
    to dropping is storing a field that would later be applied to a live
    config without anyone having asked."""
    if not isinstance(settings, dict):
        return {}
    return {k: v for k, v in settings.items() if k in PROFILE_FIELDS}


def save(model: str, endpoint: str, settings: dict, *, rate: float,
         baseline_rate: float, baseline_label: str, cases: int,
         repeats: int = 1, label: str = "") -> dict:
    """Record a measured winner. Never raises: a bookkeeping failure must not
    take down a suite that has just spent real quota to produce this."""
    clean = sanitize(settings)
    row = {
        "settings": clean,
        "label": label or ", ".join(f"{k}={v}" for k, v in sorted(clean.items())),
        "rate": round(float(rate), 4),
        "baseline_rate": round(float(baseline_rate), 4),
        "baseline": baseline_label,
        "cases": int(cases),
        "repeats": int(repeats),
        "at": time.time(),
    }
    try:
        with _LOCK:
            data = _read()
            data["profiles"][bucket_key(model, endpoint)] = row
            _write(data)
    except Exception:
        pass
    return row


def get(model: str, endpoint: str) -> dict | None:
    """The measured profile for this model on this endpoint, or None.

    None means "nobody has measured this", which is the ordinary case and must
    stay distinguishable from "measured, and the defaults won" -- the latter is
    stored with an empty `settings`, so it stops the question being reopened.
    """
    try:
        with _LOCK:
            data = _read()
        row = data["profiles"].get(bucket_key(model, endpoint))
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def apply_to(cfg, model: str, endpoint: str) -> list:
    """Apply the measured profile to `cfg` in place.

    Returns the list of "field=value" strings actually changed, so the caller
    can say them out loud. An empty list means nothing changed -- either
    there is no profile, or the config already matches it -- and the caller
    must stay silent in that case rather than announcing a no-op.

    Types are taken from the attribute already on the config, the same rule
    `evals._apply_overrides` uses: a profile that set a string where an int
    lives would be a setting nobody reads, showing up later as "the flag made
    no difference" and being believed.
    """
    row = get(model, endpoint)
    if not row:
        return []
    changed = []
    for key, value in sanitize(row.get("settings") or {}).items():
        if not hasattr(cfg, key):
            continue                      # a knob this build no longer has
        current = getattr(cfg, key)
        try:
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, str):
                value = str(value)
        except (TypeError, ValueError):
            continue
        if current == value:
            continue
        setattr(cfg, key, value)
        changed.append(f"{key}={value}")
    return changed


def describe(model: str, endpoint: str) -> str:
    """One line for the user, or "" when nothing has been measured."""
    row = get(model, endpoint)
    if not row:
        return ""
    gain = (row.get("rate", 0.0) - row.get("baseline_rate", 0.0)) * 100
    what = row.get("label") or "the defaults"
    return (f"{what} — {row.get('rate', 0):.0%} vs {row.get('baseline_rate', 0):.0%} "
            f"for {row.get('baseline') or 'the baseline'} "
            f"({gain:+.0f} points over {row.get('cases', 0)} case(s))")


def forget(model: str = "", endpoint: str = "") -> None:
    """Drop one profile, or all of them when no model is named."""
    try:
        with _LOCK:
            if not model:
                PROFILE_FILE.unlink(missing_ok=True)
                return
            data = _read()
            if data["profiles"].pop(bucket_key(model, endpoint), None) is not None:
                _write(data)
    except Exception:
        pass


def all_profiles() -> dict:
    """Everything measured so far, for the Settings panel."""
    try:
        with _LOCK:
            return dict(_read()["profiles"])
    except Exception:
        return {}
