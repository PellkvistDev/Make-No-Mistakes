"""Generate the shared constants block inside mobile/agent-core.js.

Ten subsystems in this project are written twice -- once in Python for the
desktop, once in JavaScript for the phone -- and the only thing holding the two
copies equal is a set of tests that shell out to node and compare them. That
catches drift; it does not prevent it, and CLAUDE.md names four of these pairs
as "must stay in step", which is another way of saying it has recurred.

Most of that surface is DATA, not behaviour: wire-format version bytes, key
derivation parameters, repository and branch names, sample rates, tool schemas,
prompt text. Data can have one source. This writes the JavaScript for it, so
the values cannot disagree -- the class of bug goes away rather than being
caught one instance at a time.

WHY A BLOCK INSIDE agent-core.js, and not a generated file of its own:

The phone has no build step, deliberately -- it is a folder of static files
with no toolchain to rot. A separate file would need a script tag in
index.html, an entry in the service worker's precache list, and a module
lookup that works both under a plain <script> and under `node --test`. Three
new ways to half-deploy a phone, to save one file. A marked block needs none
of them: agent-core.js keeps loading exactly as it did.

The hand-written code around the block still has to be reviewed as normal. What
is inside it never does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "mobile" / "agent-core.js"

# Runnable as `python scripts/gen_mobile_core.py` from anywhere, without the
# repo having to be installed or on PYTHONPATH -- the point of a one-command
# regenerate is that the command always works.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BEGIN = "  /* ==== GENERATED — do not edit by hand ==="
END = "  /* ==== END GENERATED ==="

REGEN = "python scripts/gen_mobile_core.py"


def _js(value) -> str:
    """A JS literal. json.dumps is the right tool: JSON is a subset of JS
    object syntax, and it escapes exactly what needs escaping in a string."""
    return json.dumps(value, ensure_ascii=False, indent=2).replace("\n", "\n  ")


def collect() -> list:
    """(name, value, comment) for everything the phone should not restate.

    Deliberately values only. Anything with behaviour -- the crypto, the agent
    loop, the sync store -- stays hand-written on both sides and pinned by the
    node tests; generating logic across two languages is a different and much
    worse idea than generating the numbers it operates on.
    """
    from glmcode import live, pairing, syncstore, tools
    from glmcode.prompts import (HONEST_REPORT_RULE, RESUME_PREAMBLE,
                                 UNTRUSTED_INPUT_RULE)

    return [
        ("PBKDF2_ITERS", syncstore.PBKDF2_ITERS,
         "Key derivation. Both devices unlock the same vault, so this is not a "
         "number either side may tune on its own."),
        ("RECOVERY_ALPHABET", syncstore.RECOVERY_ALPHABET,
         "No lookalike characters: this gets read off one screen and typed on "
         "another."),
        ("RECOVERY_GROUPS", syncstore.RECOVERY_GROUPS, ""),
        ("RECOVERY_GROUP_LEN", syncstore.RECOVERY_GROUP_LEN, ""),

        ("PAIR_WIRE_V", pairing._WIRE_V,
         "The pairing envelope. A mismatch here surfaces as \"that pairing link "
         "is damaged\" on a phone whose desktop is working perfectly."),
        ("PAIR_WIRE_V_PLAIN", pairing._WIRE_V_PLAIN,
         "Still READ, so a phone that updated ahead of its desktop can pair."),
        ("PAIR_SALT_LEN", pairing._SALT_LEN, ""),
        ("PAIR_IV_LEN", pairing._IV_LEN, ""),
        ("PAIR_CODE_ALPHABET", pairing.CODE_ALPHABET, ""),
        ("PAIR_CODE_LENGTH", pairing.CODE_LENGTH, ""),

        ("SYNC_REPO_NAME", syncstore.SYNC_REPO_NAME,
         "Where the encrypted chats live. Both devices must look in the same "
         "place or each has a store the other cannot see."),
        ("SYNC_REPO_BRANCH", syncstore.SYNC_REPO_BRANCH, ""),
        ("STATE_BRANCH", syncstore.STATE_BRANCH, "Legacy per-repo store."),
        ("DEVICE_LOCK_TTL_MS", syncstore.DEVICE_LOCK_TTL_MS,
         "How long a device's claim on a chat stands. The whole point of the "
         "lock is that both devices agree when it has expired."),
        ("DEVICE_LOCK_HEARTBEAT_S", syncstore.DEVICE_LOCK_HEARTBEAT_S, ""),

        ("LIVE_INPUT_RATE", live.INPUT_SAMPLE_RATE,
         "Two different rates, in and out. Using one for both is a chipmunk or "
         "a drawl depending which way round you get it wrong."),
        ("LIVE_OUTPUT_RATE", live.OUTPUT_SAMPLE_RATE, ""),
        ("LIVE_INPUT_MIME", live.INPUT_MIME, ""),

        ("UNTRUSTED_INPUT_RULE", UNTRUSTED_INPUT_RULE,
         "A security rule that holds on one device and not the other is worth "
         "much less than it looks: both work on the same repository."),

        ("HONEST_REPORT_RULE", HONEST_REPORT_RULE,
         "Reported of the desktop's sub-agents -- \"they fail a task but they "
         "lie and say they succeed\" -- and the phone's workers are the same "
         "kind of thing answering to the same person. A reporting rule that "
         "holds on one device and not the other buys very little."),

        ("RESUME_PREAMBLE", RESUME_PREAMBLE,
         "What a resumed sub-agent is told. Both devices resume workers, and "
         "the framing is the whole of why it carries on rather than "
         "re-summarising the report it just gave -- a paraphrase on one side "
         "is a different instruction with nothing to say so."),

        ("WORKER_SCHEMAS", tools.CONVERSATIONAL_SCHEMAS,
         "The descriptions ARE the interface -- they decide whether the model "
         "dispatches a worker or tries the job inline. A paraphrase on one "
         "device makes the two behave differently for no visible reason."),
    ]


def render() -> str:
    lines = [
        BEGIN + "================================================",
        "   *",
        "   * Written by " + REGEN,
        "   * from glmcode/{syncstore,pairing,live,tools,prompts}.py.",
        "   *",
        "   * These are values both devices must agree on exactly. Editing them",
        "   * here only makes the phone disagree with the desktop; change the",
        "   * Python and regenerate. tests/test_generated_core.py fails if this",
        "   * block is stale.",
        "   */",
    ]
    for name, value, comment in collect():
        if comment:
            lines.append("")
            for chunk in _wrap(comment):
                lines.append(f"  // {chunk}")
        lines.append(f"  const {name} = {_js(value)};")
    lines.append(END + "==============================================*/")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int = 72) -> list:
    words, out, line = text.split(), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def splice(source: str, block: str) -> str:
    start = source.find(BEGIN)
    if start == -1:
        raise SystemExit(
            f"{TARGET.name} has no generated block. Add the markers first:\n"
            f"  {BEGIN}...\n  {END}...")
    end = source.find(END, start)
    if end == -1:
        raise SystemExit(f"{TARGET.name}: generated block is not closed")
    end = source.find("\n", source.find("*/", end)) + 1
    return source[:start] + block + source[end:]


def generate() -> str:
    return splice(TARGET.read_text(encoding="utf-8"), render())


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv
    wanted = generate()
    if TARGET.read_text(encoding="utf-8") == wanted:
        print(f"{TARGET.relative_to(ROOT)} is up to date.")
        return 0
    if check:
        print(f"{TARGET.relative_to(ROOT)} is STALE. Run: {REGEN}", file=sys.stderr)
        return 1
    TARGET.write_text(wanted, encoding="utf-8")
    print(f"Wrote {TARGET.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
