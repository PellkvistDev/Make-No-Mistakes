"""Web Push: how the desktop tells the phone something finished.

The whole architecture is built around one constraint -- the phone cannot work
in the background, so it hands off to the desktop and the desktop finishes the
turn. That works. And the phone was never told: you found out by opening the
app and looking.

Web Push is exactly enough to close that. It cannot RUN anything on the phone
(WebKit has never shipped Background Sync or Background Fetch, and this does
not change that), but it can deliver a notification, and a notification is the
entire missing piece.

It also keeps the no-backend rule, which is what makes it fit rather than
merely work: the desktop is the sender. There is no server in the middle. The
subscription travels to the desktop inside the encrypted sync store the two
devices already share, and from then on the desktop posts straight to whatever
push service the phone's browser named -- Apple's for an installed iOS PWA,
Google's for Chrome.

Two specs, both implemented here rather than pulled in as a dependency:

  RFC 8291  the message encryption. The push service is an untrusted relay: it
            sees the endpoint and the ciphertext and never the text. Keys come
            from an ECDH with the subscription's own public key, so only that
            device can read it.
  RFC 8292  VAPID. A signed JWT identifying the sender, which is what stops
            anyone who learns an endpoint from pushing to it.

`pywebpush` would do this, but it pulls in http-ece, py-vapid and their
dependency trees for about two hundred lines, and this project ships with four
runtime dependencies on purpose. `cryptography` is already one of them.

RFC 8291 §5 publishes a complete worked example -- keys, salt, plaintext and
the exact bytes out. tests/test_webpush.py runs it, so this is checked against
the specification itself rather than against my reading of it.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import struct
import time
from hashlib import sha256

# The push service is told how big a record may be; one record is all we send.
RECORD_SIZE = 4096
# How long a VAPID assertion stays valid. Kept short: it is minted per send.
JWT_TTL_SECONDS = 12 * 60 * 60
# The push service holds the message this long if the device is offline.
DEFAULT_TTL = 24 * 60 * 60


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64url(text: str) -> bytes:
    text = str(text or "").strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256, one block. Every derivation in RFC 8291 asks for 32 bytes
    or fewer, so the counter never goes past 0x01 and the general form would be
    unused code on a security path."""
    prk = hmac.new(salt, ikm, sha256).digest()
    return hmac.new(prk, info + b"\x01", sha256).digest()[:length]


def _p256():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.SECP256R1()


def generate_keys() -> dict:
    """A VAPID keypair for this desktop. Generated once and kept; the public
    half is handed to the phone at subscribe time and the push service pins the
    subscription to it, so rotating it silently invalidates every subscription
    already out there."""
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(_p256())
    return {
        "private": b64url(key.private_numbers().private_value.to_bytes(32, "big")),
        "public": b64url(_public_bytes(key.public_key())),
    }


def _public_bytes(public_key) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat)
    return public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def _load_private(raw: str):
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.derive_private_key(int.from_bytes(unb64url(raw), "big"), _p256())


def _load_public(raw: bytes):
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.EllipticCurvePublicKey.from_encoded_point(_p256(), raw)


def encrypt(payload: bytes, ua_public: bytes, auth_secret: bytes,
            *, as_private=None, salt: bytes | None = None) -> bytes:
    """One aes128gcm record, RFC 8291 §3.

    as_private and salt are parameters only so the RFC's worked example can be
    reproduced exactly; in use both are freshly random per message, which is
    required -- reusing a key/salt pair across two messages to the same
    subscription reuses the AES-GCM nonce, and that leaks the plaintext.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if as_private is None:
        as_private = ec.generate_private_key(_p256())
    if salt is None:
        salt = os.urandom(16)

    as_public = _public_bytes(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), _load_public(ua_public))

    # The subscription's own keys are bound into the derivation, so a record is
    # readable by that device and nothing else -- including the push service,
    # which is an untrusted relay by design.
    ikm = _hkdf(auth_secret, shared,
                b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    # 0x02 delimits the LAST record. 0x01 would say "more follow", and a
    # receiver that then finds none reports a decryption failure.
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!L", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def vapid_headers(endpoint: str, private_key: str, public_key: str,
                  subject: str = "mailto:makenomistakes@localhost",
                  now: float | None = None) -> dict:
    """The Authorization header for one send, RFC 8292.

    `aud` is the ORIGIN of the endpoint, not the endpoint itself. Sending the
    full URL is the classic mistake here and the push service answers 401 with
    nothing that says which field it disliked.
    """
    from urllib.parse import urlsplit

    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature)
    from cryptography.hazmat.primitives.hashes import SHA256

    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now if now is not None else time.time()) + JWT_TTL_SECONDS),
        "sub": subject,
    }
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"},
                               separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":"),
                             sort_keys=True).encode())
    signing_input = f"{header}.{body}".encode("ascii")

    der = _load_private(private_key).sign(signing_input, ec.ECDSA(SHA256()))
    # JWS wants the raw r||s pair, fixed width. DER is variable-length and
    # would be rejected as a malformed signature.
    r, s = decode_dss_signature(der)
    signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return {
        "Authorization": f"vapid t={header}.{body}.{b64url(signature)}, k={public_key}",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(DEFAULT_TTL),
    }


def send(subscription: dict, message: dict, keys: dict, *, timeout: int = 10,
         session=None) -> dict:
    """Deliver one notification. Never raises -- a missed toast must not break
    whatever the caller was actually doing.

    Returns {"ok": bool, "status": int, "gone": bool}. `gone` means the push
    service says this subscription is dead (404/410): the phone uninstalled the
    app or the browser dropped it, and the caller should forget it rather than
    retry it forever.
    """
    import requests

    endpoint = str((subscription or {}).get("endpoint") or "")
    sub_keys = (subscription or {}).get("keys") or {}
    if not endpoint or not sub_keys.get("p256dh") or not sub_keys.get("auth"):
        return {"ok": False, "status": 0, "gone": False,
                "error": "incomplete subscription"}
    try:
        body = encrypt(json.dumps(message, separators=(",", ":")).encode("utf-8"),
                       unb64url(sub_keys["p256dh"]), unb64url(sub_keys["auth"]))
        headers = vapid_headers(endpoint, keys["private"], keys["public"])
        post = (session or requests).post
        resp = post(endpoint, data=body, headers=headers, timeout=timeout)
    except Exception as e:
        return {"ok": False, "status": 0, "gone": False, "error": str(e)}
    return {
        "ok": 200 <= resp.status_code < 300,
        "status": resp.status_code,
        # 404 the endpoint never existed, 410 it has been revoked. Both mean
        # stop trying; anything else may be transient.
        "gone": resp.status_code in (404, 410),
    }
