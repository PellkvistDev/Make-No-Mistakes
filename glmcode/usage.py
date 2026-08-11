"""How many requests this machine has made to each model today.

The free tiers that matter here are metered in requests per day, not tokens,
and the numbers are small enough to run out inside one task: Google's free tier
gives Gemini 3.6 Flash twenty requests a day, and an agentic turn spends
several. Someone should be able to see that coming instead of discovering it as
a 429 halfway through.

Nothing here talks to a provider. There is no endpoint that reports free-tier
consumption, so this counts what WE sent -- which is the only number this app
can know first-hand. It will drift from the provider's own count if the same
key is used elsewhere, and it says so rather than pretending to be authoritative.
"""

from __future__ import annotations

import json
import threading
from datetime import date

from .config import CONFIG_DIR

USAGE_FILE = CONFIG_DIR / "model_usage.json"

# One lock for the process. Sub-agents run in threads and share the file, so
# without this a parallel spawn loses counts to a read-modify-write race.
_LOCK = threading.Lock()


def _today() -> str:
    # Local midnight. Google resets on Pacific time and other providers differ,
    # so this is an approximation and the UI says "today" rather than claiming
    # to know when the provider's own window turns over.
    return date.today().isoformat()


def _read() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"day": _today(), "counts": {}}
    if not isinstance(data, dict) or data.get("day") != _today():
        return {"day": _today(), "counts": {}}    # a new day starts from zero
    counts = data.get("counts")
    return {"day": data["day"],
            "counts": counts if isinstance(counts, dict) else {}}


def record(model: str) -> None:
    """Count one request. Never raises: a counter must not break a turn."""
    if not model:
        return
    try:
        with _LOCK:
            data = _read()
            data["counts"][model] = int(data["counts"].get(model, 0)) + 1
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def today() -> dict:
    """{model: requests} so far today."""
    try:
        with _LOCK:
            return dict(_read()["counts"])
    except Exception:
        return {}


def reset() -> None:
    try:
        with _LOCK:
            USAGE_FILE.unlink()
    except Exception:
        pass
