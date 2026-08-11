"""Tests for QR pairing (glmcode.pairing).

Same split as the sync tests: the pure logic runs anywhere, the real AES-GCM
roundtrip only where the native crypto backend is healthy.

The security claim under test is narrow and worth stating: a captured QR alone
must be useless, because the pairing code is shown as text and never encoded
into the image. So the token must carry no plaintext, a wrong code must fail
rather than return garbage, and the payload must ride in the URL fragment where
no server ever sees it.
"""

import math
import time

import pytest

from glmcode import pairing, syncstore

needs_crypto = pytest.mark.skipif(
    not syncstore.crypto_available(), reason="cryptography AES-GCM unavailable")


# ------------------------------------------------------------- pairing code

def test_code_avoids_the_characters_people_misread():
    assert not (set("IO01") & set(pairing.CODE_ALPHABET))


def test_code_is_the_advertised_length_and_alphabet():
    for _ in range(20):
        code = pairing.make_code()
        assert len(code) == pairing.CODE_LENGTH
        assert set(code) <= set(pairing.CODE_ALPHABET)


def test_codes_do_not_repeat():
    """secrets, not random — a predictable code would undo the whole scheme."""
    assert len({pairing.make_code() for _ in range(200)}) > 190


def test_code_carries_enough_entropy_to_be_worth_having():
    bits = pairing.CODE_LENGTH * math.log2(len(pairing.CODE_ALPHABET))
    assert bits >= 28, "a shorter code would make a captured QR cheap to crack"


def test_code_is_long_enough_for_the_key_derivation():
    """derive_key refuses anything under 6 characters, so a shorter code would
    break sealing outright rather than merely weaken it."""
    assert pairing.CODE_LENGTH >= 6


def test_normalize_accepts_sloppy_typing():
    assert pairing.normalize_code(" nw89-gr ") == "NW89GR"
    assert pairing.normalize_code("NW 89 GR") == "NW89GR"
    assert pairing.normalize_code("") == ""


def test_normalize_does_not_silently_rewrite_a_typo():
    """Turning one valid code into another would replace a clear failure with a
    baffling one."""
    assert pairing.normalize_code("NW89GX") == "NW89GX"


# ------------------------------------------------------------------ payload

def test_payload_omits_secrets_that_do_not_exist():
    p = pairing.build_payload(model_key="zai-x")
    assert p["modelKey"] == "zai-x"
    # An empty string would read as "configured" on the phone.
    assert "githubToken" not in p and "syncPass" not in p


def test_payload_carries_everything_the_phone_needs():
    p = pairing.build_payload(model_key="k", base_url="https://api", model="m",
                              github_token="t", sync_passphrase="s")
    assert {"modelKey", "baseUrl", "model", "githubToken", "syncPass"} <= set(p)


def test_payload_expires():
    p = pairing.build_payload(model_key="k", now=1000.0)
    assert p["exp"] == int((1000.0 + pairing.PAIR_TTL_SECONDS) * 1000)


# ------------------------------------------------------------------- the URL

def test_token_rides_in_the_fragment_so_no_server_sees_it():
    url = pairing.pair_url("https://you.github.io/app/", "TOK")
    assert url == "https://you.github.io/app/#pair=TOK"


def test_pair_url_replaces_any_existing_fragment():
    assert pairing.pair_url("https://x/app/#stale", "TOK").endswith("/app/#pair=TOK")


def test_pair_url_without_an_app_url_is_empty_not_broken():
    assert pairing.pair_url("", "TOK") == ""


# -------------------------------------------------------- seal / open (real)

@needs_crypto
def test_sealed_token_leaks_no_plaintext():
    code = pairing.make_code()
    token = pairing.seal(pairing.build_payload(
        model_key="zai-SECRET-KEY", github_token="github_pat_SECRET",
        sync_passphrase="my sync phrase"), code)
    assert "zai-SECRET-KEY" not in token
    assert "github_pat_SECRET" not in token
    assert "my sync phrase" not in token


@needs_crypto
def test_roundtrip_recovers_the_payload_despite_sloppy_typing():
    code = pairing.make_code()
    token = pairing.seal(pairing.build_payload(
        model_key="k", github_token="t", sync_passphrase="s"), code)
    back = pairing.open_sealed(token, f" {code.lower()} ")
    assert back["modelKey"] == "k" and back["githubToken"] == "t" and back["syncPass"] == "s"


@needs_crypto
def test_a_captured_qr_is_useless_without_the_code():
    """The point of the whole design: the code is displayed, never encoded."""
    token = pairing.seal(pairing.build_payload(model_key="k"), pairing.make_code())
    with pytest.raises(syncstore.SyncError):
        pairing.open_sealed(token, pairing.make_code())


@needs_crypto
def test_a_stale_pairing_link_is_refused():
    code = pairing.make_code()
    token = pairing.seal(pairing.build_payload(model_key="k"), code)
    with pytest.raises(syncstore.SyncError, match="expired"):
        pairing.open_sealed(token, code, now=time.time() + pairing.PAIR_TTL_SECONDS + 1)


@needs_crypto
def test_a_damaged_link_fails_clearly():
    with pytest.raises(syncstore.SyncError, match="damaged"):
        pairing.open_sealed("not-a-real-token", "ABCDEF")


@needs_crypto
def test_each_seal_uses_a_fresh_salt():
    code = pairing.make_code()
    p = pairing.build_payload(model_key="k")
    assert pairing.seal(p, code) != pairing.seal(p, code)


# --------------------------------------------------------------------- #
# Every provider, not just the primary.
#
# An API added on the desktop had no route to the phone: the payload carried
# one baseUrl and one model, so the phone's whole idea of "which API" was
# whatever it was paired with. The alternative to this was typing base URLs
# into a phone by hand, or putting keys in the synced repo -- the one place
# they must never go.

def _prov(name, base, key="k", models=("m",)):
    return {"name": name, "base_url": base, "api_key": key,
            "models": list(models)}


def test_all_configured_providers_are_offered_to_the_phone():
    got = pairing.providers_for_phone([
        _prov("Google AI Studio", "https://generativelanguage.googleapis.com/v1beta/openai",
              "sk-g", ["gemini-3.6-flash"]),
        _prov("Z.AI", "https://api.z.ai/api/paas/v4", "sk-z"),
    ])
    assert [p["name"] for p in got] == ["Google AI Studio", "Z.AI"]
    assert got[0]["key"] == "sk-g"
    assert got[0]["models"] == ["gemini-3.6-flash"]


def test_a_provider_on_this_machine_is_not_sent_to_the_phone():
    """The phone cannot reach the desktop's loopback address. Pairing one over
    would put an entry in the phone's menu that fails on selection with a
    connection error and nothing explaining why."""
    got = pairing.providers_for_phone([
        _prov("Ollama", "http://localhost:11434/v1"),
        _prov("Also local", "http://127.0.0.1:1234/v1"),
        _prov("Google", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ])
    assert [p["name"] for p in got] == ["Google"]


def test_a_provider_with_no_key_is_not_sent():
    got = pairing.providers_for_phone([_prov("Unconfigured", "https://x.test/v1", "")])
    assert got == []


def test_providers_ride_along_in_the_sealed_payload():
    provs = pairing.providers_for_phone(
        [_prov("Google", "https://generativelanguage.googleapis.com/v1beta/openai", "sk-g")])
    payload = pairing.build_payload(model_key="sk-g", providers=provs)
    code = pairing.make_code()
    back = pairing.open_sealed(pairing.seal(payload, code), code)
    assert back["providers"][0]["baseUrl"].endswith("/v1beta/openai")
    assert back["providers"][0]["key"] == "sk-g"


def test_an_absent_provider_list_is_not_sent_as_an_empty_one():
    """Same rule as every other secret here: a field that is not there must not
    arrive as something the phone treats as configured."""
    assert "providers" not in pairing.build_payload(model_key="k")
    assert "providers" not in pairing.build_payload(model_key="k", providers=[])


# ---- the phone's side of the same contract --------------------------------

import json as _json
import pathlib as _pathlib
import shutil as _shutil
import subprocess as _subprocess

_CORE = _pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
_needs_node = pytest.mark.skipif(
    not (_shutil.which("node") and _CORE.is_file()), reason="node unavailable")


def _merge(existing, incoming):
    js = _subprocess.run(
        ["node", "-e",
         "const C=require(process.argv[1]);"
         "console.log(JSON.stringify(C.mergeProviders("
         "JSON.parse(process.argv[2]),JSON.parse(process.argv[3]))));",
         str(_CORE), _json.dumps(existing), _json.dumps(incoming)],
        capture_output=True, text=True, timeout=30)
    assert js.returncode == 0, js.stderr
    return _json.loads(js.stdout)


@_needs_node
def test_scanning_again_adds_an_api_without_removing_one():
    """Re-scanning is the whole feature, so merging is the whole job: replacing
    would throw away anything set up on the phone, and ignoring the incoming
    list would make the second scan do nothing."""
    got = _merge(
        [{"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "z"}],
        [{"name": "Google", "baseUrl": "https://g.test/v1", "key": "g"}])
    assert [p["name"] for p in got] == ["Z.AI", "Google"]


@_needs_node
def test_a_rotated_key_wins_on_the_next_scan():
    got = _merge(
        [{"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "old"}],
        [{"name": "Z.AI", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "new"}])
    assert len(got) == 1 and got[0]["key"] == "new"


@_needs_node
def test_a_field_the_desktop_did_not_send_does_not_blank_one_the_phone_has():
    """Absent means "nothing to say", not "no key" -- the same rule the sync
    store needed, for the same reason."""
    got = _merge(
        [{"name": "Z.AI", "baseUrl": "https://z.test/v1", "key": "keep",
          "models": ["a"]}],
        [{"name": "Z.AI", "baseUrl": "https://z.test/v1"}])
    assert got[0]["key"] == "keep"
    assert got[0]["models"] == ["a"]


@_needs_node
def test_the_same_endpoint_is_one_provider_however_it_is_spelled():
    """Matched on URL, not name: the name is a label the desktop may change --
    it did, when the primary stopped being called "z.ai (free)" -- while the
    URL is what identifies an endpoint. A trailing slash is not a new API."""
    got = _merge(
        [{"name": "z.ai (free)", "baseUrl": "https://api.z.ai/api/paas/v4", "key": "k"}],
        [{"name": "Z.AI", "baseUrl": "https://API.Z.AI/api/paas/v4/", "key": "k2"}])
    assert len(got) == 1
    assert got[0]["name"] == "Z.AI" and got[0]["key"] == "k2"
