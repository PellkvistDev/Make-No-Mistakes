"""The phone's shared constants are generated, so they cannot disagree.

Ten subsystems in this project are written twice, and the only thing holding
the two copies equal is a set of tests that shell out to node and compare them.
That catches drift after the fact. It does not prevent it, and CLAUDE.md names
four of these pairs as "must stay in step" -- which is another way of saying it
has already recurred four times.

Values can have one source. scripts/gen_mobile_core.py writes the JavaScript
for the ones both devices must agree on exactly -- wire-format version bytes,
key-derivation parameters, repo and branch names, sample rates, the worker tool
schemas, the untrusted-input rule -- and this file fails if the committed block
no longer matches what the Python says today.

Behaviour is deliberately NOT generated. The crypto, the agent loop and the
sync store stay hand-written on both sides and pinned by the node tests;
generating logic across two languages is a different and much worse idea than
generating the numbers it operates on.
"""

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "gen_mobile_core.py"
CORE = ROOT / "mobile" / "agent-core.js"

sys.path.insert(0, str(ROOT / "scripts"))
import gen_mobile_core as gen  # noqa: E402

needs_node = pytest.mark.skipif(
    not (shutil.which("node") and CORE.is_file()),
    reason="node or mobile/agent-core.js unavailable")


def test_the_committed_block_is_what_the_python_says_today():
    """The whole mechanism. If this fails, nobody has to work out WHICH value
    drifted -- the fix is one command, and it is in the failure message."""
    assert CORE.read_text(encoding="utf-8") == gen.generate(), (
        "mobile/agent-core.js is out of date with the Python it is generated "
        "from. Run: python scripts/gen_mobile_core.py")


def test_check_mode_reports_staleness_without_writing(tmp_path):
    """CI runs --check. It must not "fix" the tree and go green on a change
    nobody committed."""
    before = CORE.read_text(encoding="utf-8")
    assert gen.main(["--check"]) == 0
    assert CORE.read_text(encoding="utf-8") == before


def test_a_stale_block_fails_check(monkeypatch, capsys):
    """Proving the guard bites. Without this, --check passing means only that
    it ran."""
    monkeypatch.setattr(gen, "generate", lambda: "something else entirely")
    assert gen.main(["--check"]) == 1
    assert "STALE" in capsys.readouterr().err


def test_regenerating_is_idempotent():
    once = gen.generate()
    assert gen.splice(once, gen.render()) == once


def test_the_block_is_marked_as_generated():
    """Anyone editing it by hand should be told, in the file, that their change
    will be overwritten and will only make the phone disagree with the
    desktop."""
    body = CORE.read_text(encoding="utf-8")
    start = body.index(gen.BEGIN)
    header = body[start:start + 700]
    assert "do not edit by hand" in header
    assert "scripts/gen_mobile_core.py" in header


# ------------------------------------------ the values actually reach JS ---

def _node(expr: str):
    out = subprocess.run(
        ["node", "-e",
         f"const C=require(process.argv[1]); console.log(JSON.stringify({expr}));",
         str(CORE)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_generated_values_are_the_ones_the_phone_actually_uses():
    """Generating a constant the module never exports would look identical to
    generating one it does -- right up until the phone used its own copy."""
    from glmcode import syncstore
    assert _node("C.PBKDF2_ITERS") == syncstore.PBKDF2_ITERS
    assert _node("C.SYNC_REPO_NAME") == syncstore.SYNC_REPO_NAME
    assert _node("C.SYNC_REPO_BRANCH") == syncstore.SYNC_REPO_BRANCH
    assert _node("C.DEVICE_LOCK_TTL_MS") == syncstore.DEVICE_LOCK_TTL_MS


@needs_node
def test_the_worker_schemas_are_now_python_s_own():
    from glmcode.tools import CONVERSATIONAL_SCHEMAS
    assert _node("C.WORKER_SCHEMAS") == json.loads(json.dumps(CONVERSATIONAL_SCHEMAS))


@needs_node
def test_the_untrusted_rule_still_reaches_the_prompt():
    """It is generated as a constant, but what matters is that the hand-written
    prompt still concatenates it -- a generated value nothing references is a
    rule that is not in force."""
    from glmcode.prompts import UNTRUSTED_INPUT_RULE
    assert UNTRUSTED_INPUT_RULE in _node("C.SYSTEM_PROMPT")


@needs_node
def test_the_live_rates_did_not_get_swapped():
    """Two different numbers in the two directions; using one for both is a
    chipmunk or a drawl depending which way round you get it wrong."""
    from glmcode import live
    assert _node("C.LIVE_INPUT_RATE") == live.INPUT_SAMPLE_RATE == 16000
    assert _node("C.LIVE_OUTPUT_RATE") == live.OUTPUT_SAMPLE_RATE == 24000


# --------------------------------------------------- nothing is restated ---

@pytest.mark.parametrize("name", sorted(n for n, _, _ in gen.collect()))
def test_no_hand_written_copy_survives_beside_the_generated_one(name):
    """A second `const X = ...` outside the block would shadow or conflict with
    the generated one depending where it sat, and would be the exact bug this
    change exists to remove."""
    body = CORE.read_text(encoding="utf-8")
    outside = body[:body.index(gen.BEGIN)] + body[body.index(gen.END):]
    assert f"const {name} " not in outside and f"const {name}=" not in outside, \
        f"{name} is still defined by hand outside the generated block"


def test_only_data_is_generated():
    """The line this stays on. Generating a FUNCTION across two languages is a
    different and much worse idea than generating the numbers it operates on,
    and the failure mode -- subtly different behaviour on one device -- is
    exactly what the whole exercise is meant to end."""
    for name, value, _ in gen.collect():
        assert isinstance(value, (str, int, float, bool, list, dict)), \
            f"{name} is not plain data ({type(value).__name__})"
