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
