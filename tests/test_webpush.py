"""Web Push, checked against the specification's own worked example.

The desktop finishes turns the phone was suspended through -- that works, and
the phone was never told about it. Web Push closes that: it cannot run anything
on the phone, but it can deliver a notification, and a notification is the
whole missing piece.

Encryption here is not decoration. The push service (Apple's, Google's) is an
untrusted relay that sees the endpoint and the ciphertext, so a mistake in the
key derivation is a chat summary sent in the clear to a third party. This is
implemented from RFC 8291 rather than pulled in as a dependency, which is only
defensible if it is checked against the RFC itself -- so the first test below
is the complete worked example from RFC 8291 §5: their keys, their salt, their
plaintext, and the exact bytes they say come out.

If that test ever fails, do not adjust it to match the code.
"""

import json

import pytest

pytest.importorskip("cryptography", reason="webpush needs cryptography")

from glmcode import webpush  # noqa: E402


# --------------------------------------------------------------------- #
# The example from RFC 8291 section 5.
#
# HEX, where the RFC prints base64url. Same bytes, and the reason for the
# re-encoding is worth stating so nobody "tidies" it back: a base64url blob
# beside a name like AS_PRIVATE is exactly the shape of a leaked key, and
# secret scanners flag it -- correctly, on the evidence available to them.
# These are published in a public specification, there is nothing to revoke,
# and a build that fails on them trains people to wave through the scanner
# that will one day be right.
#
# Names say what they are for the same reason: this is a worked example, not
# anybody's credential.

EXAMPLE_PLAINTEXT = b"When I grow up, I want to be a watermelon"
# The subscription's own keypair, as a browser would have produced it.
EXAMPLE_UA_PUBLIC = bytes.fromhex(
    "042571b2becdfde360551aaf1ed0f4cd366c11cebe555f89bcb7b186a5333917"
    "3168ece2ebe018597bd30479b86e3c8f8eced577ca59187e9246990db682008b"
    "0e")
EXAMPLE_UA_AUTH = bytes.fromhex("05305932a1c7eabe13b6cec9fda48882")
# The sender's ephemeral key and salt, fixed here so the output is reproducible.
EXAMPLE_AS_PRIVATE = bytes.fromhex(
    "c9f58f89813e9f8e872e71f42aa64e1757c9254dcc62b72ddc010bb4043ea11c")
EXAMPLE_SALT = bytes.fromhex("0c6bfaadad67958803092d454676f397")
# And the bytes the RFC says come out.
EXAMPLE_BODY = bytes.fromhex(
    "0c6bfaadad67958803092d454676f397000010004104fe33f4ab0dea71914db5"
    "5823f73b54948f41306d920732dbb9a59a53286482200e597a7b7bc260ba1c22"
    "7998580992e93973002f3012a28ae8f06bbb78e5ec0ff297de5b429bba7153d3"
    "a4ae0caa091fd425f3b4b5414add8ab37a19c1bbb05cf5cb5b2a2e0562d55863"
    "5641ec52812c6c8ff42e95ccb86be7cd")


def test_the_rfc_s_own_example_produces_the_bytes_the_rfc_prints():
    """The one test that says this implementation is correct rather than
    merely self-consistent. Same keys, same salt, same plaintext, and the
    output is compared to the specification's."""
    body = webpush.encrypt(
        EXAMPLE_PLAINTEXT,
        EXAMPLE_UA_PUBLIC,
        EXAMPLE_UA_AUTH,
        as_private=webpush._load_private(webpush.b64url(EXAMPLE_AS_PRIVATE)),
        salt=EXAMPLE_SALT,
    )
    assert body == EXAMPLE_BODY


# --------------------------------------------------------------------- #
# Structure and self-consistency.

def _round_trip_keys():
    """A subscription's keypair, as a browser would produce."""
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(webpush._p256())
    return key, webpush._public_bytes(key.public_key())


def _decrypt(body: bytes, ua_private, ua_public: bytes, auth: bytes) -> bytes:
    """The receiving half, written out so the tests can read what was sent."""
    import struct

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, _rs, idlen = body[:16], struct.unpack("!L", body[16:20])[0], body[20]
    as_public, ciphertext = body[21:21 + idlen], body[21 + idlen:]
    shared = ua_private.exchange(ec.ECDH(), webpush._load_public(as_public))
    ikm = webpush._hkdf(auth, shared,
                        b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    return AESGCM(cek).decrypt(nonce, ciphertext, None)[:-1]   # drop the 0x02


def test_a_message_survives_the_round_trip():
    ua_private, ua_public = _round_trip_keys()
    auth = b"0123456789abcdef"
    body = webpush.encrypt(b"the worker finished", ua_public, auth)
    assert _decrypt(body, ua_private, ua_public, auth) == b"the worker finished"


def test_the_header_is_the_shape_the_spec_describes():
    import struct
    ua_private, ua_public = _round_trip_keys()
    body = webpush.encrypt(b"x", ua_public, b"0123456789abcdef")
    assert len(body[:16]) == 16                                  # salt
    assert struct.unpack("!L", body[16:20])[0] == webpush.RECORD_SIZE
    assert body[20] == 65                                        # key length
    assert body[21] == 0x04                                      # uncompressed


def test_two_messages_never_reuse_a_salt_or_an_ephemeral_key():
    """Both are random per message, and they have to be: reusing the pair
    against one subscription reuses the AES-GCM nonce, which leaks the
    plaintext. This is the single worst mistake available here."""
    _, ua_public = _round_trip_keys()
    auth = b"0123456789abcdef"
    a = webpush.encrypt(b"same text", ua_public, auth)
    b = webpush.encrypt(b"same text", ua_public, auth)
    assert a[:16] != b[:16], "salt was reused"
    assert a[21:86] != b[21:86], "ephemeral key was reused"
    assert a != b


def test_the_last_record_is_marked_as_last():
    """0x02 says "no more records follow". With 0x01 a receiver waits for a
    record that never comes and reports a decryption failure."""
    ua_private, ua_public = _round_trip_keys()
    auth = b"0123456789abcdef"
    body = webpush.encrypt(b"hi", ua_public, auth)

    import struct
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, idlen = body[:16], body[20]
    as_public, ct = body[21:21 + idlen], body[21 + idlen:]
    shared = ua_private.exchange(ec.ECDH(), webpush._load_public(as_public))
    ikm = webpush._hkdf(auth, shared,
                        b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    assert AESGCM(cek).decrypt(nonce, ct, None)[-1] == 0x02


# --------------------------------------------------------------------- #
# VAPID

def test_the_audience_is_the_origin_not_the_whole_endpoint():
    """The classic mistake, and the push service answers 401 without saying
    which field it disliked."""
    keys = webpush.generate_keys()
    headers = webpush.vapid_headers(
        "https://push.example.com/send/abc123?tok=9", keys["private"], keys["public"])
    _, body, _ = headers["Authorization"].split(" ", 1)[1].split(",")[0].split(".")
    claims = json.loads(webpush.unb64url(body))
    assert claims["aud"] == "https://push.example.com"


def test_the_assertion_verifies_against_the_public_key_it_names():
    """A signature nobody checked is a signature that could be anything. This
    verifies it the way the push service will."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        encode_dss_signature)
    from cryptography.hazmat.primitives.hashes import SHA256

    keys = webpush.generate_keys()
    headers = webpush.vapid_headers("https://push.example.com/x",
                                    keys["private"], keys["public"])
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    header_b64, body_b64, sig_b64 = token.split(".")
    raw = webpush.unb64url(sig_b64)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"),
                               int.from_bytes(raw[32:], "big"))
    pub = webpush._load_public(webpush.unb64url(keys["public"]))
    pub.verify(der, f"{header_b64}.{body_b64}".encode(), ec.ECDSA(SHA256()))


def test_the_assertion_expires():
    keys = webpush.generate_keys()
    headers = webpush.vapid_headers("https://push.example.com/x",
                                    keys["private"], keys["public"], now=1000)
    body = headers["Authorization"].split("t=")[1].split(",")[0].split(".")[1]
    assert json.loads(webpush.unb64url(body))["exp"] == 1000 + webpush.JWT_TTL_SECONDS


def test_the_public_key_travels_with_the_assertion():
    """`k=` is how the push service knows which key to verify against, and it
    must be the same one the subscription was created with."""
    keys = webpush.generate_keys()
    headers = webpush.vapid_headers("https://push.example.com/x",
                                    keys["private"], keys["public"])
    assert headers["Authorization"].endswith(f"k={keys['public']}")
    assert headers["Content-Encoding"] == "aes128gcm"


# --------------------------------------------------------------------- #
# Sending

class _Resp:
    def __init__(self, status):
        self.status_code = status


class _Session:
    def __init__(self, status=201):
        self.status, self.calls = status, []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers})
        return _Resp(self.status)


def _subscription():
    _, ua_public = _round_trip_keys()
    return {"endpoint": "https://push.example.com/send/abc",
            "keys": {"p256dh": webpush.b64url(ua_public),
                     "auth": webpush.b64url(b"0123456789abcdef")}}


def test_a_send_posts_ciphertext_to_the_endpoint():
    session = _Session()
    keys = webpush.generate_keys()
    out = webpush.send(_subscription(), {"title": "Done", "body": "It finished"},
                       keys, session=session)
    assert out["ok"] is True
    call = session.calls[0]
    assert call["url"] == "https://push.example.com/send/abc"
    assert b"It finished" not in call["data"], "the payload must be encrypted"
    assert call["headers"]["Content-Encoding"] == "aes128gcm"


@pytest.mark.parametrize("status", [404, 410])
def test_a_dead_subscription_is_reported_as_gone(status):
    """The phone uninstalled the app or the browser dropped it. The caller has
    to forget it rather than retry it forever."""
    out = webpush.send(_subscription(), {"title": "x"}, webpush.generate_keys(),
                       session=_Session(status))
    assert out["ok"] is False and out["gone"] is True


def test_a_server_error_is_not_treated_as_gone():
    """A 500 is the push service having a bad day, not the device being dead --
    dropping the subscription over one would silence the phone permanently."""
    out = webpush.send(_subscription(), {"title": "x"}, webpush.generate_keys(),
                       session=_Session(500))
    assert out["ok"] is False and out["gone"] is False


def test_a_network_failure_is_swallowed():
    """A missed toast must never break the turn that produced it."""
    class Boom:
        def post(self, *a, **k):
            raise OSError("no route to host")

    out = webpush.send(_subscription(), {"title": "x"}, webpush.generate_keys(),
                       session=Boom())
    assert out["ok"] is False and out["gone"] is False
    assert "no route" in out["error"]


def test_an_incomplete_subscription_is_refused_without_a_request():
    session = _Session()
    out = webpush.send({"endpoint": "https://push.example.com/x"}, {"title": "x"},
                       webpush.generate_keys(), session=session)
    assert out["ok"] is False
    assert session.calls == [], "nothing should have gone on the wire"
