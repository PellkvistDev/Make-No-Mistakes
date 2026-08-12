"""The phone's list of APIs, against the desktop's.

The desktop grew a provider catalogue and the phone never got it: its setup
screen asked for a "z.ai / model API key" and offered a base-URL menu holding
two z.ai entries, so a phone set up by hand could not reach anything else --
whatever the rest of the app supported. Voice, which only Google can do, was
unreachable on a phone that had not been paired from a computer.

The catalogue is duplicated rather than fetched, because the page has no Python
behind it and fetching it would need a network before you have a key. So the
two copies are compared here instead. Not a full equality check: the phone
needs a subset (where to point, what to call the model, where the key comes
from) and carries its own wording.
"""

import json
import pathlib
import shutil
import subprocess

import pytest

from glmcode import providers

CORE_JS = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
needs_node = pytest.mark.skipif(
    not (shutil.which("node") and CORE_JS.is_file()),
    reason="node or mobile/agent-core.js unavailable")


def _phone():
    out = subprocess.run(
        ["node", "-e",
         "const C=require(process.argv[1]);"
         "console.log(JSON.stringify(C.SETUP_PRESETS));", str(CORE_JS)],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return {p["key"]: p for p in json.loads(out.stdout)}


@needs_node
def test_the_phone_offers_every_hosted_api_the_desktop_does():
    """The failure this exists to stop: an API you can configure on the
    computer but not on the phone."""
    want = {c["key"] for c in providers.choices()
            if not c["local"] and c["key"] != "custom"}
    assert want <= set(_phone()), "an API the desktop offers is missing here"


@needs_node
def test_it_still_offers_typing_one_in_by_hand():
    assert "custom" in _phone()


@needs_node
@pytest.mark.parametrize("key", ["zai", "google"])
def test_the_endpoint_and_the_default_model_match_the_desktop(key):
    """A base URL that has drifted is the worst kind of wrong here: it fails
    at the first message, long after setup, with an error about the key."""
    theirs = _phone()[key]
    ours = providers.preset(key)
    assert theirs["baseUrl"] == ours["base_url"]
    assert theirs["model"] == ours["model"]
    assert theirs["label"] == ours["label"]


@needs_node
@pytest.mark.parametrize("key", ["zai", "google"])
def test_the_key_link_points_where_the_desktop_points(key):
    """Setup is exactly where someone does not have a key yet."""
    assert _phone()[key]["keyUrl"] == providers.preset(key)["key_url"]


@needs_node
@pytest.mark.parametrize("key", ["zai", "google"])
def test_every_model_offered_is_one_the_catalogue_lists(key):
    """The phone shows a shorter menu than the desktop -- that is fine -- but
    a name it invents is one that 404s on the first message."""
    assert set(_phone()[key]["models"]) <= set(providers.preset(key)["models"])
    assert _phone()[key]["model"] in _phone()[key]["models"]


@needs_node
def test_nothing_that_runs_on_the_desktop_is_offered_to_the_phone():
    """Ollama on a computer is not reachable from a phone. Offering it is a
    menu entry that fails on selection with a connection error and nothing
    explaining why -- the same reason pairing drops local providers."""
    for key, p in _phone().items():
        assert not providers.is_local(p["baseUrl"]), f"{key} points at localhost"
    local = {c["key"] for c in providers.choices() if c["local"]}
    assert local and not (local & set(_phone())), "a local provider leaked in"


@needs_node
def test_the_one_that_can_do_voice_is_reachable_from_setup():
    """Voice needs Google, and until now the only route to a Google key on the
    phone was pairing from a computer that already had one."""
    google = _phone()["google"]
    from glmcode import live
    assert live.available(google["baseUrl"]), \
        "the setup screen must offer the API that voice needs"
