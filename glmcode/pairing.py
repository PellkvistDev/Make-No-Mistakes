"""Set the phone up by scanning, without the secrets ever touching a network.

The phone app needs a model key, a GitHub token and (for shared chats) the sync
passphrase. Typing `github_pat_...` on a phone keyboard is miserable enough that
people don't: they mail it to themselves, or lean on a cloud clipboard, or paste
it into a syncing notes app. Every one of those hands the raw secret to a third
party. That -- not careful typing -- is the real thing this replaces.

WHAT THIS DOES AND DOESN'T PROTECT
----------------------------------
The payload travels screen -> camera. It is generated locally (segno draws the
QR offline) and rides in the URL *fragment*, which browsers never put on the
wire, so it reaches the phone without any server seeing it.

It is encrypted under a short pairing code that is shown as text, NOT encoded in
the QR. So:

  * A photo or screenshot of the QR, on its own, is useless. This is the case
    worth defending: screenshots get auto-uploaded, sit in camera rolls, and
    survive in screen recordings long after the code has scrolled away.
  * Someone who can see the whole screen at the moment you pair gets both parts
    and wins. This scheme does not defend against that, and pretending it does
    would be worse than saying so.
  * The code is ~30 bits (6 chars from a 32-symbol alphabet). Combined with
    PBKDF2 at the same cost as the rest of the app, guessing it offline from a
    captured QR is expensive but not impossible for someone determined. Treat a
    leaked QR as a reason to rotate the keys, not as a shrug.

`exp` is advisory. The phone refuses a stale payload, which stops an old QR
photographed off a desk from quietly working next week -- but an attacker who
has already cracked the code can ignore the field. It is hygiene, not a control.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
import zlib

from . import syncstore

# No I/O/0/1 -- they're the characters people misread off a screen. Defined in
# syncstore and aliased here rather than spelled twice: the pairing code and the
# sync recovery code are both read off one screen and typed on another, so they
# want the same alphabet, and two copies of it is two things to keep in step.
CODE_ALPHABET = syncstore.RECOVERY_ALPHABET
CODE_LENGTH = 6
# Long enough to walk to the phone, short enough that a stale QR stops working.
PAIR_TTL_SECONDS = 300


def make_code(length: int = CODE_LENGTH) -> str:
    """A pairing code with real entropy behind it (secrets, never random)."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def normalize_code(code: str) -> str:
    """Accept what people actually type: lower case, stray spaces, a dash.

    Deliberately no cleverness beyond that. The alphabet already excludes the
    characters that get misread (I, O, 0, 1), and silently "correcting" a
    genuine typo into a different valid code would turn a clear failure into a
    confusing one."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def providers_for_phone(providers: list) -> list:
    """The providers worth sending, trimmed to what the phone can use.

    A provider on localhost is dropped. The phone cannot reach the desktop's
    loopback address, so pairing one over would hand someone a menu entry that
    fails on selection with a connection error and no explanation -- and the
    honest place to notice that is here, not on the phone.

    Providers with no key are dropped too: there is nothing to send, and an
    entry the phone cannot authenticate is the same dead end.
    """
    from . import providers as _providers
    from .config import provider_key
    out = []
    for p in providers or []:
        base = (p.get("base_url") or "").strip()
        if not base or _providers.is_local(base):
            continue
        # Through provider_key, not p["api_key"]. A provider's key normally
        # lives in an environment variable now and the field is only the
        # fallback for when writing that variable was blocked -- so reading the
        # field alone would quietly pair over an empty key for every API that
        # was set up the ordinary way.
        key = provider_key(p).strip()
        if not key:
            continue
        out.append({"name": p.get("name") or base,
                    "baseUrl": base,
                    "key": key,
                    "models": list(p.get("models") or [])})
    return out


def build_payload(*, model_key: str = "", base_url: str = "", model: str = "",
                  github_token: str = "", sync_passphrase: str = "",
                  providers: list | None = None,
                  now: float | None = None) -> dict:
    """The cleartext handed to the phone. Only what it actually needs, and only
    the fields that exist -- an absent secret must not show up as an empty
    string the phone then treats as configured."""
    out: dict = {"v": 1, "exp": int(((now if now is not None else time.time())
                                     + PAIR_TTL_SECONDS) * 1000)}
    if model_key:
        out["modelKey"] = model_key
    if base_url:
        out["baseUrl"] = base_url
    if model:
        out["model"] = model
    if github_token:
        out["githubToken"] = github_token
    if sync_passphrase:
        out["syncPass"] = sync_passphrase
    # Every configured provider, not just the primary. Scanning again later is
    # how an API added on the desktop reaches the phone -- the alternative was
    # typing a base URL and a model name into a phone by hand, or putting keys
    # in the synced repo, which is the one place they must never go.
    if providers:
        out["providers"] = providers
    return out


# version | salt | iv | ciphertext, base64url'd once. A JSON envelope with its
# fields base64'd individually and then base64'd again costs ~30% more
# characters, which is the difference between a QR a phone reads instantly and
# one it has to hunt for. mobile/agent-core.js unpacks this exact layout.
#
# Version 2 deflates the JSON before encrypting it. That is not a micro-
# optimisation: this payload is read by a phone camera, and QR size is what
# decides whether that works. A realistic payload -- two APIs with their model
# lists, a GitHub token, a sync passphrase -- is a 731-byte JSON, which came
# out as a 113-module QR once it had been base64'd and wrapped in a URL. A
# camera has to resolve every one of those modules. Deflated it is a 73-module
# code: the model names and base URLs are highly repetitive, so it compresses
# by about half, while the keys (being random) do not compress at all.
#
# Version 1 is still read, so a token minted by an older desktop still opens.
_WIRE_V = 2
_WIRE_V_PLAIN = 1
_SALT_LEN, _IV_LEN = 16, 12


def seal(payload: dict, code: str) -> str:
    """Encrypt the payload under the pairing code -> a compact URL-safe token.

    The salt travels with the ciphertext because the phone has nothing else to
    derive from; the code is what it doesn't have, and that's the point.
    """
    salt = secrets.token_bytes(_SALT_LEN)
    key = syncstore.derive_key(code, salt)
    # Compress first, encrypt second. The other order does nothing: ciphertext
    # is indistinguishable from random and deflate would only add a header.
    body = zlib.compress(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    iv, ct = syncstore.aes_encrypt_bytes(body, key)
    raw = bytes([_WIRE_V]) + salt + iv + ct
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def open_sealed(token: str, code: str, now: float | None = None) -> dict:
    """Decrypt a sealed payload. Raises SyncError on a wrong code, a mangled
    token, or one that has expired."""
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + pad)
        if (len(raw) < 1 + _SALT_LEN + _IV_LEN + 16
                or raw[0] not in (_WIRE_V, _WIRE_V_PLAIN)):
            raise ValueError("bad envelope")
        version = raw[0]
        salt = raw[1:1 + _SALT_LEN]
        iv = raw[1 + _SALT_LEN:1 + _SALT_LEN + _IV_LEN]
        ct = raw[1 + _SALT_LEN + _IV_LEN:]
    except Exception as e:
        raise syncstore.SyncError("That pairing link is damaged.") from e
    key = syncstore.derive_key(normalize_code(code), salt)
    body = syncstore.aes_decrypt_bytes(iv, ct, key)
    try:
        if version == _WIRE_V:
            body = zlib.decompress(body)
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        # Past the AES tag, so the code was right and the bytes are intact --
        # whatever is wrong here is not something a retry fixes.
        raise syncstore.SyncError("That pairing link is damaged.") from e
    ts = (now if now is not None else time.time()) * 1000
    if int(data.get("exp", 0)) < ts:
        raise syncstore.SyncError("That pairing code has expired — show a new one.")
    return data


def pair_url(app_url: str, token: str) -> str:
    """Put the token in the FRAGMENT. Browsers never send a fragment to the
    server, so the secrets reach the phone without the host ever seeing them --
    which is the whole reason this is safe to point at a public Pages URL.

    Kept for links that already exist; the QR itself no longer carries one. A
    code containing a URL is one the phone's *Camera* app will happily open,
    and on iOS that lands in Safari -- which, for an app installed to the home
    screen, is a different storage box entirely. So the keys arrive somewhere
    that is not the app, and the app still has none. The failure looked like
    the QR being unreadable and was the opposite: it read perfectly, into the
    wrong place. A bare token cannot be opened by anything, so scanning it with
    the wrong app does nothing at all, which is a far better outcome.
    """
    base = (app_url or "").strip()
    if not base:
        return ""
    return base.split("#", 1)[0] + "#pair=" + token
