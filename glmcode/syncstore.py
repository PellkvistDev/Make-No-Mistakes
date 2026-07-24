"""Cross-device session sync -- the desktop half of the phone app's Phase 1.

The desktop reads and writes the SAME encrypted store the phone uses, on a
private ORPHAN branch (``makenomistakes/state``) of a GitHub repo, so a chat
you start on your phone continues on your computer and vice versa.

Interop contract with the phone (mobile/agent-core.js) -- these MUST match byte
for byte or the two sides can't read each other:

  * Key   = PBKDF2-HMAC-SHA256(passphrase, salt, 210000 iters) -> 32 bytes.
            This is exactly what the phone's WebCrypto ``deriveKey`` produces
            (pinned by a WebCrypto vector in tests/test_syncstore.py).
  * Blob  = AES-256-GCM, fresh 12-byte IV, 16-byte tag appended to the
            ciphertext (WebCrypto's layout), base64 in {"v":1,"iv":..,"ct":..}.
            The plaintext is JSON -- json.dumps and JSON.stringify each parse
            the other's output, so whitespace differences don't matter.
  * sync.json        = {"v":1,"salt":b64,"check": <blob of "mnm-sync-ok">}
  * index.json       = <blob of {"v":1,"chats":[{id,title,updated,preview}]}>
  * chats/<id>.json  = <blob of the chat object>

SECURITY: the sync passphrase is separate from every other secret, is never
sent to GitHub (only ciphertext is), and is stored on this device through the
same secure secretstore used for the GitHub token (OS keyring, or an encrypted
file) -- never in config.json.

``cryptography`` is imported lazily so the module (and the rest of the app)
still imports where it isn't installed; sync just reports itself unavailable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from .githubsync import GitHubError
from .githubsync import _api as _github_api
from .githubsync import load_token
from .secretstore import encode_account, get_store

# Kept in lockstep with mobile/agent-core.js (STATE_BRANCH / SYNC_CHECK / PBKDF2_ITERS).
STATE_BRANCH = "makenomistakes/state"
SYNC_CHECK = "mnm-sync-ok"
PBKDF2_ITERS = 210000


class SyncError(Exception):
    """A user-safe sync failure (wrong passphrase, network, unavailable crypto)."""


# --------------------------------------------------------------------- #
# Crypto (lazy: cryptography is only needed when sync is actually used)

def crypto_available() -> bool:
    """True when AES-GCM is usable here. The UI uses this to offer/hide sync.

    Catches BaseException on purpose: a broken native `cryptography` build
    raises a Rust PanicException (a BaseException, not Exception), which a plain
    `except Exception` would let through and crash import (see secretstore.py)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        AESGCM(b"\x00" * 32)  # constructs only if the native backend is healthy
        return True
    except BaseException:
        return False


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 -> 32-byte AES key. Matches the phone's WebCrypto."""
    if not passphrase or len(str(passphrase)) < 6:
        raise SyncError("Sync passphrase must be at least 6 characters.")
    return hashlib.pbkdf2_hmac("sha256", str(passphrase).encode("utf-8"),
                               salt, PBKDF2_ITERS, 32)


def aes_encrypt(obj, key: bytes) -> dict:
    """AES-256-GCM encrypt a JSON-able object into a WebCrypto-compatible blob."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    pt = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, pt, None)  # tag is appended, like WebCrypto
    return {"v": 1, "iv": _b64e(iv), "ct": _b64e(ct)}


def aes_decrypt(blob: dict, key: bytes):
    """Reverse of aes_encrypt. Raises SyncError on a wrong key / tampering."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        pt = AESGCM(key).decrypt(_b64d(blob["iv"]), _b64d(blob["ct"]), None)
    except Exception:
        raise SyncError("Could not decrypt (wrong passphrase or corrupted data).")
    return json.loads(pt.decode("utf-8"))


# --------------------------------------------------------------------- #
# GitHub state-branch client (Contents + Git Data API)

class StateRepo:
    """Talks to a repo's ``makenomistakes/state`` branch. ``api`` is injectable
    (defaults to githubsync._api) so tests can drive it without a network."""

    def __init__(self, token: str, owner: str, repo: str,
                 branch: str = STATE_BRANCH, api=None):
        self.token, self.owner, self.repo, self.branch = token, owner, repo, branch
        self._api = api or _github_api

    def _call(self, method: str, path: str, body: dict | None = None):
        return self._api(method, path, self.token, body)

    def get_file(self, path: str) -> tuple[str, str]:
        d = self._call("GET",
                       f"/repos/{self.owner}/{self.repo}/contents/{path}?ref={self.branch}")
        content = (d.get("content") or "").replace("\n", "")
        return base64.b64decode(content).decode("utf-8"), d.get("sha")

    def put_file(self, path: str, text: str, message: str, sha: str | None = None):
        body = {"message": message, "content": _b64e(text.encode("utf-8")),
                "branch": self.branch}
        if sha:
            body["sha"] = sha
        return self._call("PUT", f"/repos/{self.owner}/{self.repo}/contents/{path}", body)

    def delete_file(self, path: str, message: str, sha: str):
        return self._call("DELETE", f"/repos/{self.owner}/{self.repo}/contents/{path}",
                          {"message": message, "sha": sha, "branch": self.branch})

    def branch_sha(self) -> str | None:
        try:
            r = self._call("GET",
                           f"/repos/{self.owner}/{self.repo}/git/ref/heads/{self.branch}")
            return (r.get("object") or {}).get("sha")
        except GitHubError:
            return None

    def create_orphan_branch(self) -> str:
        """Create the branch with NO code history -- just a marker file -- so
        session data never touches main or shows up in PRs."""
        tree = self._call("POST", f"/repos/{self.owner}/{self.repo}/git/trees",
                          {"tree": [{"path": ".mnm", "mode": "100644", "type": "blob",
                                     "content": "Make No Mistakes — session state. Do not merge.\n"}]})
        commit = self._call("POST", f"/repos/{self.owner}/{self.repo}/git/commits",
                           {"message": "Initialize Make No Mistakes state",
                            "tree": tree["sha"], "parents": []})
        self._call("POST", f"/repos/{self.owner}/{self.repo}/git/refs",
                  {"ref": f"refs/heads/{self.branch}", "sha": commit["sha"]})
        return commit["sha"]


def _read_json(repo: StateRepo, path: str) -> tuple[dict | None, str | None]:
    """(parsed JSON, sha) for a file, or (None, None) if it doesn't exist."""
    try:
        text, sha = repo.get_file(path)
    except GitHubError:
        return None, None
    try:
        return json.loads(text), sha
    except (json.JSONDecodeError, ValueError):
        return None, sha


# --------------------------------------------------------------------- #
# The encrypted store

def open_sync(repo: StateRepo, passphrase: str) -> tuple[bytes, "SyncStore", bool]:
    """Verify an existing store or bootstrap a new one. Returns (key, store,
    created). A wrong passphrase raises SyncError -- never silent garbage."""
    if not passphrase or len(str(passphrase)) < 6:
        raise SyncError("Sync passphrase must be at least 6 characters.")
    meta, _ = _read_json(repo, "sync.json")
    if meta and meta.get("v") == 1:
        key = derive_key(passphrase, _b64d(meta["salt"]))
        try:
            ok = aes_decrypt(meta["check"], key)
        except SyncError:
            raise SyncError("Wrong sync passphrase.")
        if ok != SYNC_CHECK:
            raise SyncError("Wrong sync passphrase.")
        return key, SyncStore(repo, key), False
    # First device for this repo: create the orphan branch + sync.json.
    if not repo.branch_sha():
        repo.create_orphan_branch()
    salt = os.urandom(16)
    key = derive_key(passphrase, salt)
    check = aes_encrypt(SYNC_CHECK, key)
    repo.put_file("sync.json",
                  json.dumps({"v": 1, "salt": _b64e(salt), "check": check}),
                  "Set up Make No Mistakes sync")
    return key, SyncStore(repo, key), True


class SyncStore:
    """Encrypted list/load/save/remove over one file per chat + an index."""

    def __init__(self, repo: StateRepo, key: bytes):
        self.repo, self.key = repo, key

    def _read_index(self) -> tuple[dict, str | None]:
        obj, sha = _read_json(self.repo, "index.json")
        if not obj:
            return {"v": 1, "chats": []}, None
        try:
            return aes_decrypt(obj, self.key), sha
        except SyncError:
            return {"v": 1, "chats": []}, sha

    def _write_index(self, chats: list, sha: str | None) -> None:
        blob = aes_encrypt({"v": 1, "chats": chats}, self.key)
        self.repo.put_file("index.json", json.dumps(blob), "Update session index", sha)

    def _file_sha(self, path: str) -> str | None:
        _, sha = _read_json(self.repo, path)
        return sha

    def list(self) -> list[dict]:
        """Newest-first chat summaries (id, title, updated, preview)."""
        data, _ = self._read_index()
        chats = list(data.get("chats") or [])
        chats.sort(key=lambda c: c.get("updated") or 0, reverse=True)
        return chats

    def load(self, chat_id: str) -> dict:
        obj, _ = _read_json(self.repo, f"chats/{chat_id}.json")
        if obj is None:
            raise SyncError("That chat wasn't found in the sync store.")
        return aes_decrypt(obj, self.key)

    def save(self, chat: dict) -> int:
        """Persist a chat and refresh its index entry. Stamps chat['updated']
        (epoch ms, to match the phone's Date.now())."""
        if not chat.get("id"):
            raise SyncError("chat needs an id")
        chat["updated"] = int(time.time() * 1000)
        path = f"chats/{chat['id']}.json"
        blob = aes_encrypt(chat, self.key)
        self.repo.put_file(path, json.dumps(blob),
                           f"Save session {chat['id']}", self._file_sha(path))
        data, sha = self._read_index()
        chats = [c for c in (data.get("chats") or []) if c.get("id") != chat["id"]]
        chats.append({"id": chat["id"], "title": chat.get("title") or "Untitled",
                      "updated": chat["updated"], "preview": chat.get("preview") or ""})
        self._write_index(chats, sha)
        return chat["updated"]

    def remove(self, chat_id: str) -> None:
        path = f"chats/{chat_id}.json"
        sha = self._file_sha(path)
        if sha:
            self.repo.delete_file(path, f"Delete session {chat_id}", sha)
        data, isha = self._read_index()
        self._write_index([c for c in (data.get("chats") or [])
                           if c.get("id") != chat_id], isha)


# --------------------------------------------------------------------- #
# Passphrase storage (via the secure secretstore, like the GitHub token)

def _pass_account(host: str = "github.com") -> str:
    return encode_account("sync-passphrase", host)


def save_passphrase(passphrase: str, host: str = "github.com") -> None:
    get_store().set(_pass_account(host), (passphrase or "").strip())


def load_passphrase(host: str = "github.com") -> str | None:
    return get_store().get(_pass_account(host)) or None


def forget_passphrase(host: str = "github.com") -> None:
    get_store().delete(_pass_account(host))


def open_for_repo(owner: str, repo: str, passphrase: str | None = None,
                  token: str | None = None, api=None) -> tuple[bytes, SyncStore, bool]:
    """Convenience for the app layer: resolve the stored token + passphrase and
    open the store for owner/repo."""
    if not crypto_available():
        raise SyncError("Encryption isn't available in this build "
                        "(the 'cryptography' package is required for sync).")
    token = token or load_token()
    if not token:
        raise SyncError("Connect a GitHub token first.")
    passphrase = passphrase or load_passphrase()
    if not passphrase:
        raise SyncError("Set a sync passphrase first.")
    return open_sync(StateRepo(token, owner, repo, api=api), passphrase)


# --------------------------------------------------------------------- #
# Desktop session <-> sync chat conversion (phone-compatible)

def _messages_to_transcript(messages: list) -> list[dict]:
    """Reduce OpenAI-style messages to the phone's transcript shape
    ([{role,text}] for user/assistant text), so a desktop-written chat renders
    on the phone the same way a phone-native one does."""
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text").strip()
        elif isinstance(c, str):
            text = c.strip()
        else:
            text = ""
        if text:
            out.append({"role": role, "text": text})
    return out


def session_to_chat(sess: dict) -> dict:
    """A desktop SessionStore record -> a sync chat object the phone can read.

    A leading system slot is included at index 0 because the phone overwrites
    messages[0] with its own system prompt on resume; without it, the phone
    would clobber the first real message."""
    body = [m for m in (sess.get("messages") or []) if m.get("role") != "system"]
    messages = [{"role": "system", "content": ""}] + body
    transcript = _messages_to_transcript(body)
    return {
        "id": sess["id"],
        "title": sess.get("title") or "Untitled",
        "preview": (transcript[-1]["text"][:80] if transcript else ""),
        "messages": messages,
        "transcript": transcript,
        # Desktop-only extras, namespaced so the phone simply ignores them.
        "desktop": {
            "cwd": sess.get("cwd", ""),
            "todos": sess.get("todos") or [],
            "model_provider": sess.get("model_provider", ""),
            "model": sess.get("model", ""),
        },
    }


def chat_to_session(chat: dict) -> dict:
    """A sync chat object (possibly phone-written) -> fields for a desktop
    session. Drops the leading system slot; the desktop rebuilds its own system
    prompt when the session is opened."""
    messages = [m for m in (chat.get("messages") or []) if m.get("role") != "system"]
    extra = chat.get("desktop") or {}
    return {
        "id": chat.get("id", ""),
        "title": chat.get("title") or "Untitled",
        "messages": messages,
        "cwd": extra.get("cwd", ""),
        "todos": extra.get("todos") or [],
        "model_provider": extra.get("model_provider", ""),
        "model": extra.get("model", ""),
    }
