"""Cross-device session sync: one private GitHub repo holds every synced chat.

The desktop and the phone read and write the SAME encrypted store, so a chat
you start on your phone continues on your computer and vice versa.

WHERE IT LIVES. A single dedicated private repo (SYNC_REPO_NAME), created
automatically the first time you turn sync on. Chats from every project land
there together, which is what makes the UI simple: one place to list, nothing
to pick, and a chat syncs even when its project isn't a GitHub repo at all.
(The first cut scoped the store to a branch of each connected repo; that
scattered chats across repos and forced the user to choose one before they
could see anything. STATE_BRANCH is kept only so an old per-repo store can
still be opened.)

Interop contract with the phone (mobile/agent-core.js) -- these MUST match byte
for byte or the two sides can't read each other:

  * Key   = PBKDF2-HMAC-SHA256(passphrase, salt, 210000 iters) -> 32 bytes.
            This is exactly what the phone's WebCrypto ``deriveKey`` produces
            (pinned by a WebCrypto vector in tests/test_syncstore.py).
  * Blob  = AES-256-GCM, fresh 12-byte IV, 16-byte tag appended to the
            ciphertext (WebCrypto's layout), base64 in {"v":1,"iv":..,"ct":..}.
            The plaintext is JSON -- json.dumps and JSON.stringify each parse
            the other's output, so whitespace differences don't matter.
  * sync.json          = {"v":1,"salt":b64,"check": <blob of "mnm-sync-ok">}
  * index.json         = <blob of {"v":1,"chats":[{id,title,updated,preview}]}>
  * chats/<id>.json    = <blob of the chat object>
  * chats/<id>.lock.json = <blob of {v,device_id,device,label,acquired,expires}>,
    a soft per-chat lock (see "SAME CHAT, TWO DEVICES" below).

SAME CHAT, TWO DEVICES. Sending from the phone and the desktop into the same
chat at once has no live channel to coordinate on -- each side only finds out
about the other by reading GitHub, and reads are a moment stale. The lock file
turns "silently overwrite whichever push lands last" into "tell the user,
before they send, that this chat is busy elsewhere" for the near-totality of
cases, using GitHub's own compare-and-swap (a Contents API PUT/DELETE with the
current blob sha; a stale sha is rejected) as the only real atomicity available
without a server. It is deliberately a COURTESY, not a guarantee: a lock is
held with a short TTL and renewed (heartbeat) while a turn runs, and expires on
its own if a device disappears mid-turn, so nothing ever stays wedged waiting
for a device that crashed or lost its network. The user can always override an
active lock and send anyway.

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
import secrets
import time

from .githubsync import GitHubError
from .githubsync import _api as _github_api
from .githubsync import load_token
from .secretstore import encode_account, get_store

# Kept in lockstep with mobile/agent-core.js.
SYNC_REPO_NAME = "makenomistakes-sync"   # the dedicated private data repo
SYNC_REPO_BRANCH = "main"
STATE_BRANCH = "makenomistakes/state"    # legacy per-repo store (read-only path)
SYNC_CHECK = "mnm-sync-ok"
DEVICE_LOCK_TTL_MS = 90_000               # a lock older than this is abandoned
DEVICE_LOCK_HEARTBEAT_S = DEVICE_LOCK_TTL_MS // 3000  # renew well before it expires
PBKDF2_ITERS = 210000

SYNC_REPO_README = (
    "# Make No Mistakes — synced chats\n\n"
    "This private repository stores your Make No Mistakes conversations so they\n"
    "follow you between your phone and your computer.\n\n"
    "**Everything here is encrypted on your devices** with a key derived from your\n"
    "sync passphrase, which is never sent to GitHub. Nobody with access to this\n"
    "repo — including GitHub — can read your chats without that passphrase.\n\n"
    "It's managed by the app; there's nothing to edit by hand.\n"
)


class SyncError(Exception):
    """A user-safe sync failure (wrong passphrase, network, unavailable crypto)."""


class ChatDeletedElsewhere(SyncError):
    """This chat was deleted on another device, so it must not be re-uploaded."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        super().__init__("That chat was deleted on another device.")


# How long a deletion is remembered. Long enough that every device has certainly
# seen it; bounded so the index can't grow without limit. A device that has been
# offline longer than this could still resurrect a chat -- the alternative is an
# index that never stops growing, which is worse.
TOMBSTONE_TTL_MS = 30 * 24 * 60 * 60 * 1000


def _tombstones(index: dict) -> list:
    return list((index or {}).get("deleted") or [])


def _is_deleted(gone: list, chat_id: str) -> bool:
    """Deleting is permanent for that id.

    Not "newest write wins": the device that still has the chat open writes
    LATER than the delete by definition, so letting the newer write win is
    exactly the bug -- the chat comes back, and keeps coming back after every
    turn. Chat ids are uuids and never reused, so nothing legitimate is lost.
    """
    return any(t.get("id") == chat_id for t in gone)


def _prune_tombstones(gone: list, now: int | None = None) -> list:
    cutoff = (now if now is not None else _now_ms()) - TOMBSTONE_TTL_MS
    return [t for t in gone if int(t.get("at") or 0) >= cutoff]


class LockedElsewhere(SyncError):
    """Another device holds a live lock on this chat right now."""

    def __init__(self, device_label: str, since_ms: int):
        self.device_label = device_label
        self.since_ms = since_ms
        super().__init__(f"This chat is active on {device_label} right now.")


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------- #
# Crypto (lazy: cryptography is only needed when sync is actually used)

INSTALL_HINT = "python -m pip install --user cryptography"


def crypto_status() -> tuple[str, str]:
    """(state, human message) for AES-GCM availability, where state is
    'ok' | 'missing' | 'broken'.

    `cryptography` only became a listed requirement when sync shipped, so the
    common case by far is an install that predates it -- which deserves the
    exact command to fix it, not a dead end.

    Catches BaseException on purpose: a broken native build raises a Rust
    PanicException (a BaseException, not Exception), which a plain
    `except Exception` would let through and crash the caller (see
    secretstore.py, which hit the same thing)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        AESGCM(b"\x00" * 32)  # constructs only if the native backend is healthy
        return "ok", ""
    except ImportError:
        return "missing", (
            "Chat sync needs the 'cryptography' package, which isn't installed yet "
            "(it was added to the requirements when sync shipped). Re-run install.ps1, "
            f"or: {INSTALL_HINT}")
    except BaseException as e:
        return "broken", (
            "The 'cryptography' package is installed but its native backend won't "
            f"load here ({type(e).__name__}), so chat sync is unavailable. "
            f"Reinstalling usually fixes it: {INSTALL_HINT} --force-reinstall")


def crypto_available() -> bool:
    """True when AES-GCM is usable here. The UI uses this to offer/hide sync."""
    return crypto_status()[0] == "ok"


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


def aes_encrypt_bytes(data: bytes, key: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM over raw bytes -> (iv, ciphertext-with-tag).

    Pairing needs this: its payload is compressed before encryption, and
    routing those bytes through the JSON-shaped helper below would base64 them
    first and give most of the compression straight back."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    return iv, AESGCM(key).encrypt(iv, data, None)  # tag appended, like WebCrypto


def aes_decrypt_bytes(iv: bytes, ct: bytes, key: bytes) -> bytes:
    """Reverse of aes_encrypt_bytes. Raises SyncError on a wrong key / tampering."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        return AESGCM(key).decrypt(iv, ct, None)
    except Exception:
        raise SyncError("Could not decrypt (wrong passphrase or corrupted data).")


def aes_encrypt(obj, key: bytes) -> dict:
    """AES-256-GCM encrypt a JSON-able object into a WebCrypto-compatible blob."""
    iv, ct = aes_encrypt_bytes(
        json.dumps(obj, ensure_ascii=False).encode("utf-8"), key)
    return {"v": 1, "iv": _b64e(iv), "ct": _b64e(ct)}


def aes_decrypt(blob: dict, key: bytes):
    """Reverse of aes_encrypt. Raises SyncError on a wrong key / tampering."""
    pt = aes_decrypt_bytes(_b64d(blob["iv"]), _b64d(blob["ct"]), key)
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

    def list_dir(self, path: str) -> list[dict]:
        """Names and shas of the files in one directory, or [] if there is no
        such directory. Used only to rebuild the index -- normal operation
        never enumerates, which is exactly why losing the index used to be
        permanent."""
        try:
            d = self._call("GET",
                           f"/repos/{self.owner}/{self.repo}/contents/{path}?ref={self.branch}")
        except GitHubError:
            return []
        return [e for e in d if isinstance(e, dict)] if isinstance(d, list) else []

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


def _missing(e: GitHubError) -> bool:
    """Whether a failed read means the file genuinely is not there.

    Only a status we can read AND that is 404 counts as absent. A 500, a
    timeout, an unreachable network (status 0) mean "there may well be a file
    here and we did not get it", which is a different answer entirely -- see
    ``SyncStore._read_index``. A status of None is a fake API in a test, or an
    older path that raises without one; those are treated as absent, which is
    what they have always meant.
    """
    st = getattr(e, "status", None)
    return st is None or st == 404


def _index_row(chat: dict) -> dict:
    """The summary the index carries for one chat.

    Built in one place because ``save`` and ``rebuild_index`` both produce it,
    and a rebuild that dropped a field would quietly downgrade every row in the
    store to whatever the rebuild happened to know about.
    """
    return {
        "id": chat.get("id") or "",
        "title": chat.get("title") or "Untitled",
        "updated": int(chat.get("updated") or 0),
        "preview": chat.get("preview") or "",
        "project": chat.get("project") or "",
        # Just the full_name, not the whole repo object: the index is read in
        # one piece on every list, so it stays small. Empty means the chat has
        # no GitHub repo, which is what lets the phone say so in the list
        # instead of only finding out once you've tapped it.
        "repo": ((chat.get("repo") or {}).get("full_name") or ""),
        "device": chat.get("device") or "",
        # So a machine can find turns left unfinished without decrypting every
        # chat in the store. The index is a cache -- an older client that saves
        # without this field drops it from the row while the chat body keeps it
        # -- so it is a shortlist to load from, never the decision.
        "interrupted": bool(chat.get("interrupted")),
    }


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
    # First device: make sure the branch exists, then write sync.json. The
    # dedicated repo is created with a README so `main` already exists; the
    # orphan path only matters for the legacy per-repo store.
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

    # ---------------------------------------------------------------- #
    # Push subscriptions.
    #
    # How the desktop reaches a phone that is not running. The subscription is
    # a URL the push service will accept messages at, plus the keys that
    # encrypt them -- so it goes in the encrypted store like everything else,
    # never in the index (which is a cache read far more often than this).
    #
    # A list, not a single value: one desktop can be paired with more than one
    # phone, and a phone that reinstalls arrives as a new endpoint rather than
    # an update to the old one.

    PUSH_PATH = "devices/push.json"
    # The desktop's VAPID public key, which the phone needs BEFORE it can
    # subscribe. It travels here rather than in the pairing QR: that payload is
    # read by a camera and its module count is what decides whether scanning
    # works at all, so adding a field to it spends a margin that was measured.
    VAPID_PATH = "devices/vapid.json"

    def vapid_public_key(self) -> str:
        obj, _ = _read_json(self.repo, self.VAPID_PATH)
        if not obj:
            return ""
        try:
            return str(aes_decrypt(obj, self.key).get("public") or "")
        except SyncError:
            return ""

    def set_vapid_public_key(self, public_key: str) -> None:
        """Publish it, but only when it changed. This is called on a timer, and
        a write per tick would be a commit per tick in the user's sync repo."""
        if not public_key or self.vapid_public_key() == public_key:
            return
        _, sha = _read_json(self.repo, self.VAPID_PATH)
        blob = aes_encrypt({"v": 1, "public": public_key}, self.key)
        self.repo.put_file(self.VAPID_PATH, json.dumps(blob),
                           "Publish push key", sha)

    def push_subscriptions(self) -> list:
        obj, _ = _read_json(self.repo, self.PUSH_PATH)
        if not obj:
            return []
        try:
            data = aes_decrypt(obj, self.key)
        except SyncError:
            return []
        subs = data.get("subscriptions")
        return subs if isinstance(subs, list) else []

    def add_push_subscription(self, subscription: dict) -> int:
        """Register a device, replacing any entry with the same endpoint.

        Keyed on the endpoint because that is what identifies a device to the
        push service: re-subscribing on the same phone (a new key, a browser
        that rotated it) must update the row rather than leave a stale one
        behind that every later send fails against.
        """
        endpoint = str((subscription or {}).get("endpoint") or "")
        if not endpoint:
            raise SyncError("a push subscription needs an endpoint")
        subs = [s for s in self.push_subscriptions()
                if s.get("endpoint") != endpoint]
        subs.append(dict(subscription))
        return self._write_push(subs)

    def drop_push_subscription(self, endpoint: str) -> int:
        """Forget a device the push service says is gone (404/410). Retrying a
        dead endpoint forever is the alternative."""
        subs = [s for s in self.push_subscriptions()
                if s.get("endpoint") != endpoint]
        return self._write_push(subs)

    def _write_push(self, subs: list) -> int:
        _, sha = _read_json(self.repo, self.PUSH_PATH)
        blob = aes_encrypt({"v": 1, "subscriptions": subs}, self.key)
        self.repo.put_file(self.PUSH_PATH, json.dumps(blob),
                           "Update push devices", sha)
        return len(subs)

    def _read_index(self) -> tuple[dict, str | None]:
        """The index, or an empty one if there genuinely isn't one yet.

        There are THREE outcomes here and this used to collapse them into two,
        which cost more than it looks:

        - Not there. A first device against a fresh store. Empty index, no sha.
        - There, and we could not fetch it -- GitHub down, no network. Answering
          "you have no chats" to that is a lie the user acts on, and it is the
          most common of the three.
        - There, and unreadable. This one was destructive: it returned an empty
          index carrying the REAL sha, so the next save wrote that empty index
          straight over the good one. Nothing enumerates ``chats/`` in normal
          operation, so every other chat became unreachable on both devices at
          once -- ciphertext still sitting there, with nothing left that knew
          the ids. ``rebuild_index`` exists because of this, but it can only
          help if the rows are still there to rebuild FROM.

        The index is a cache and the bodies are the truth, so refusing to
        proceed costs one failed sync; guessing cost the whole list.
        """
        try:
            text, sha = self.repo.get_file("index.json")
        except GitHubError as e:
            if _missing(e):
                return {"v": 1, "chats": [], "deleted": []}, None
            raise SyncError(f"Couldn't read the chat index: {e}") from e
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            raise SyncError("The chat index is damaged. Your chats are still "
                            "stored; rebuilding the index will find them again.")
        try:
            data = aes_decrypt(obj, self.key)
        except SyncError:
            raise SyncError("The chat index couldn't be decrypted. Your chats are "
                            "still stored; rebuilding the index will find the ones "
                            "this key can read.")
        data.setdefault("deleted", [])   # indexes written before tombstones
        return data, sha

    def _write_index(self, chats: list, sha: str | None,
                     deleted: list | None = None) -> None:
        payload = {"v": 1, "chats": chats, "deleted": list(deleted or [])}
        blob = aes_encrypt(payload, self.key)
        self.repo.put_file("index.json", json.dumps(blob), "Update session index", sha)

    def _file_sha(self, path: str) -> str | None:
        _, sha = _read_json(self.repo, path)
        return sha

    def list(self) -> list[dict]:
        """Newest-first chat summaries (id, title, updated, preview)."""
        data, _ = self._read_index()
        gone = _tombstones(data)
        chats = [c for c in (data.get("chats") or [])
                 if not _is_deleted(gone, c.get("id"))]
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
        # Another device may have deleted this chat while we still had it open.
        # Saving would quietly bring it back -- and it would keep coming back,
        # since every later turn re-uploads it. A delete has to outrank a write
        # that started before it.
        idx, _ = self._read_index()
        if _is_deleted(_tombstones(idx), chat["id"]):
            raise ChatDeletedElsewhere(chat["id"])
        path = f"chats/{chat['id']}.json"
        # Merge over whatever is already stored, rather than replacing it.
        #
        # A write only carries the fields the writing device knows about, and
        # the two ends of this store are different programs released
        # separately -- the desktop is routinely older than the phone, or the
        # other way round. Replacing wholesale means the older side silently
        # deletes every field the newer one added, which is how a chat lost the
        # repo it belonged to and how another lost its transcript.
        # Absent means "I have nothing to say about this", not "delete it".
        # Clearing something is still possible by sending it explicitly: the
        # desktop empties `pending` by passing [], which is present and wins.
        existing, sha = _read_json(self.repo, path)
        merged = chat
        if existing is not None:
            try:
                merged = {**aes_decrypt(existing, self.key), **chat}
            except SyncError:
                merged = chat   # unreadable (rotated key): this write is the truth
        blob = aes_encrypt(merged, self.key)
        self.repo.put_file(path, json.dumps(blob),
                           f"Save session {chat['id']}", sha)
        data, sha = self._read_index()
        chats = [c for c in (data.get("chats") or []) if c.get("id") != chat["id"]]
        # From the MERGED body, not the incoming write. A device that sends no
        # `repo` (or no title) is saying nothing about it, and building the row
        # from the write alone blanked that column in the list while the body
        # kept it -- the same bug the merge above exists to prevent, left
        # half-fixed one field further out.
        chats.append(_index_row(merged))
        self._write_index(chats, sha, _tombstones(data))
        return chat["updated"]

    def rebuild_index(self) -> dict:
        """Rebuild the index from the chat bodies. Returns {found, unreadable}.

        The index is a cache and the bodies are the truth -- that is stated all
        over this file, and it was not true of the store, because nothing could
        turn the bodies back into a list. A damaged or wrongly-overwritten
        index left readable ciphertext that no device could name.

        - **A chat is only dropped when its body says so.** Rows are rebuilt
          from what is actually stored; a body that will not decrypt is counted
          and LEFT ALONE, never deleted, since "this key can't read it" is not
          "it isn't wanted".
        - **Tombstones are preserved when the old index can still be read**,
          and only then. Losing them would let a chat deleted on another device
          come back on the next save -- so a rebuild after total loss reports
          what it did rather than pretending the delete history survived.
        - **The lock files are skipped**: `chats/<id>.lock.json` sits in the
          same directory and is not a chat.
        """
        try:
            old, _ = self._read_index()
        except SyncError:
            old = {}                       # unreadable: that is why we are here
        found, unreadable = [], 0
        for entry in self.repo.list_dir("chats"):
            name = entry.get("name") or ""
            if not name.endswith(".json") or name.endswith(".lock.json"):
                continue
            chat_id = name[:-len(".json")]
            try:
                chat = self.load(chat_id)
            except (SyncError, GitHubError):
                unreadable += 1
                continue
            chat.setdefault("id", chat_id)
            found.append(_index_row(chat))
        found.sort(key=lambda r: r.get("updated") or 0, reverse=True)
        _, sha = _read_json(self.repo, "index.json")
        self._write_index(found, sha, _prune_tombstones(_tombstones(old)))
        return {"found": len(found), "unreadable": unreadable}

    def remove(self, chat_id: str) -> None:
        path = f"chats/{chat_id}.json"
        sha = self._file_sha(path)
        if sha:
            self.repo.delete_file(path, f"Delete session {chat_id}", sha)
        data, isha = self._read_index()
        gone = [t for t in _tombstones(data) if t.get("id") != chat_id]
        gone.append({"id": chat_id, "at": _now_ms()})
        self._write_index([c for c in (data.get("chats") or [])
                           if c.get("id") != chat_id], isha,
                          _prune_tombstones(gone))

    # ------------------------------------------------------------- locking --
    def _lock_path(self, chat_id: str) -> str:
        return f"chats/{chat_id}.lock.json"

    def _read_lock(self, chat_id: str) -> tuple[dict | None, str | None]:
        """(live lock data or None, the raw file's sha or None). The sha is
        returned even for an EXPIRED lock -- the caller needs it to overwrite
        the stale file via a sha-gated PUT."""
        obj, sha = _read_json(self.repo, self._lock_path(chat_id))
        if not obj:
            return None, None
        try:
            data = aes_decrypt(obj, self.key)
        except SyncError:
            return None, sha
        if data.get("expires", 0) < _now_ms():
            return None, sha   # abandoned -- treat as free, but keep the sha
        return data, sha

    def check_lock(self, chat_id: str) -> dict | None:
        """Current lock info if the chat is actively held by ANY device
        (including this one), or None if free. Read-only -- does not acquire."""
        data, _ = self._read_lock(chat_id)
        return data

    def acquire_lock(self, chat_id: str, device_id: str, device_label: str,
                     force: bool = False) -> dict:
        """Atomically claim the chat for `device_id`, via a sha-gated write --
        so if two devices race to acquire at once, exactly one wins and the
        other finds out immediately (as a GitHub 409, translated below) rather
        than both believing they hold it. Raises LockedElsewhere if another
        device holds a live lock and `force` isn't set; `force` claims it
        anyway (an explicit user override, not a merge)."""
        current, sha = self._read_lock(chat_id)
        if current and current.get("device_id") != device_id and not force:
            raise LockedElsewhere(current.get("label") or current.get("device") or "another device",
                                  current.get("acquired", 0))
        now = _now_ms()
        payload = {"v": 1, "device_id": device_id, "device": device_label,
                  "label": device_label, "acquired": now, "expires": now + DEVICE_LOCK_TTL_MS}
        blob = aes_encrypt(payload, self.key)
        try:
            self.repo.put_file(self._lock_path(chat_id), json.dumps(blob),
                              f"Lock {chat_id}", sha)
        except GitHubError:
            # Someone else's write landed in the gap between our read and ours.
            winner, _ = self._read_lock(chat_id)
            if winner and winner.get("device_id") != device_id:
                raise LockedElsewhere(winner.get("label") or winner.get("device") or "another device",
                                      winner.get("acquired", 0))
            raise
        return payload

    def renew_lock(self, chat_id: str, device_id: str, device_label: str) -> bool:
        """Heartbeat: extend the TTL while this device still holds the lock.
        Returns False only when another device has genuinely taken over (so
        the caller can warn its user their turn was preempted) -- a transient
        write failure returns True, since "couldn't confirm" must never read
        as "definitely lost it"."""
        current, sha = self._read_lock(chat_id)
        if current and current.get("device_id") != device_id:
            return False
        now = _now_ms()
        payload = {"v": 1, "device_id": device_id, "device": device_label,
                  "label": device_label, "acquired": now, "expires": now + DEVICE_LOCK_TTL_MS}
        blob = aes_encrypt(payload, self.key)
        try:
            self.repo.put_file(self._lock_path(chat_id), json.dumps(blob),
                              f"Renew lock {chat_id}", sha)
            return True
        except GitHubError:
            winner, _ = self._read_lock(chat_id)
            if winner and winner.get("device_id") != device_id:
                return False
            return True

    def release_lock(self, chat_id: str, device_id: str) -> None:
        """Best-effort: if this fails or is skipped, the lock still self-heals
        via TTL expiry within DEVICE_LOCK_TTL_MS."""
        current, sha = self._read_lock(chat_id)
        if not sha:
            return
        if current and current.get("device_id") != device_id:
            return   # already taken over by someone else -- not ours to clear
        try:
            self.repo.delete_file(self._lock_path(chat_id), f"Unlock {chat_id}", sha)
        except GitHubError:
            pass


# --------------------------------------------------------------------- #
# Passphrase storage (via the secure secretstore, like the GitHub token)

def _pass_account(host: str = "github.com") -> str:
    return encode_account("sync-passphrase", host)


# The alphabet the pairing code already uses, for the same reason: these are
# the characters that survive being read off a screen and typed somewhere else.
RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
RECOVERY_GROUPS, RECOVERY_GROUP_LEN = 4, 5


def make_passphrase() -> str:
    """A sync passphrase nobody has to invent.

    Asking a person to make one up was the wrong question in the first place.
    It is not a password anyone ever types to log in -- it is the key the chats
    are encrypted under, and it has exactly one job: be the same on both
    devices. Pairing already carries it to the phone, so the only thing the
    human input added was a chance to pick something weak, or to mistype it on
    the second device and quietly fork the history into two unreadable halves.

    Grouped with dashes because the one time it IS read by a human -- bringing
    a second computer in -- it gets copied by eye. 20 characters from a
    32-symbol alphabet is 100 bits, which is far past anything a passphrase
    someone thought up would carry.
    """
    return "-".join(
        "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_GROUP_LEN))
        for _ in range(RECOVERY_GROUPS))


def central_has_store(token: str | None = None, api=None) -> bool:
    """Whether a sync store already exists in the dedicated repo.

    This is what separates "first device, generate a key" from "second device,
    and the key is already decided". Generating one against an existing store
    would fail as `Wrong sync passphrase` -- technically correct and completely
    unhelpful, since the user never chose a passphrase to get wrong.
    """
    token = token or load_token()
    if not token:
        raise SyncError("Connect a GitHub token first.")
    owner, name = ensure_sync_repo(token, api=api)
    repo = StateRepo(token, owner, name, branch=SYNC_REPO_BRANCH, api=api)
    meta, _ = _read_json(repo, "sync.json")
    return bool(meta and meta.get("v") == 1)


def save_passphrase(passphrase: str, host: str = "github.com") -> None:
    get_store().set(_pass_account(host), (passphrase or "").strip())


def load_passphrase(host: str = "github.com") -> str | None:
    return get_store().get(_pass_account(host)) or None


def forget_passphrase(host: str = "github.com") -> None:
    get_store().delete(_pass_account(host))


def ensure_sync_repo(token: str, api=None) -> tuple[str, str]:
    """(owner, name) of the dedicated private sync repo, creating it if needed.

    Created with auto_init so `main` exists immediately, and with a README that
    explains what the repo is -- someone browsing their GitHub shouldn't find an
    unexplained repo full of ciphertext."""
    api = api or _github_api
    me = api("GET", "/user", token)
    owner = (me or {}).get("login") or ""
    if not owner:
        raise SyncError("GitHub didn't return your account name.")
    try:
        api("GET", f"/repos/{owner}/{SYNC_REPO_NAME}", token)
        return owner, SYNC_REPO_NAME      # already there
    except GitHubError:
        pass                               # 404 -> create it below
    try:
        api("POST", "/user/repos", token, {
            "name": SYNC_REPO_NAME, "private": True, "auto_init": True,
            "description": "Encrypted Make No Mistakes chat sync (managed by the app).",
        })
    except GitHubError as e:
        raise SyncError(
            f"Couldn't create the private sync repository '{SYNC_REPO_NAME}': {e} "
            "Your token needs Administration: Read and write (to create repos) "
            "and Contents: Read and write.")
    # Make sure the README landed, so `main` exists before we write to it.
    repo = StateRepo(token, owner, SYNC_REPO_NAME, branch=SYNC_REPO_BRANCH, api=api)
    if not repo.branch_sha():
        try:
            repo.put_file("README.md", SYNC_REPO_README, "Add Make No Mistakes sync README")
        except GitHubError:
            pass
    return owner, SYNC_REPO_NAME


def open_central(passphrase: str | None = None, token: str | None = None,
                 api=None) -> tuple[bytes, SyncStore, bool]:
    """Open (creating if needed) the one private sync repo. This is what the app
    uses -- no repo to pick, and it works even for projects that aren't on
    GitHub at all."""
    state, why = crypto_status()
    if state != "ok":
        raise SyncError(why)
    token = token or load_token()
    if not token:
        raise SyncError("Connect a GitHub token first.")
    passphrase = passphrase or load_passphrase()
    if not passphrase:
        raise SyncError("Set a sync passphrase first.")
    owner, name = ensure_sync_repo(token, api=api)
    repo = StateRepo(token, owner, name, branch=SYNC_REPO_BRANCH, api=api)
    return open_sync(repo, passphrase)


def open_for_repo(owner: str, repo: str, passphrase: str | None = None,
                  token: str | None = None, api=None) -> tuple[bytes, SyncStore, bool]:
    """Open the LEGACY per-repo store (a state branch inside a project repo).
    Kept so chats written by the first version of sync are still reachable."""
    state, why = crypto_status()
    if state != "ok":
        raise SyncError(why)
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


def project_label(cwd: str) -> str:
    """A short, human name for the project a chat belongs to (the folder name).
    Shown in the synced-chat lists so chats from different projects are still
    tellable apart now that they all share one repo."""
    s = (cwd or "").replace("\\", "/").rstrip("/")
    return s.rsplit("/", 1)[-1] if s else ""


# --- device handoff -------------------------------------------------------
# A synced chat carries its whole message history between devices, and the two
# devices do NOT have the same tools: the desktop has a shell, tests and a
# browser; the phone has the GitHub API and nothing that executes. The model
# learns from its own history, so after a handoff it will happily imitate a
# turn it "successfully" ran on the other device and call a tool that doesn't
# exist here -- reaching for PowerShell on the phone, or for view_image the way
# the phone resolves it on the desktop.
#
# Rebuilding the system prompt per device isn't enough on its own: the prompt
# says one thing while a hundred lines of history demonstrate the opposite, and
# demonstrations win. So on every cross-device open we splice a marker into the
# history at the boundary, naming the switch and voiding the earlier tool calls
# as examples.
#
# The marker is a SYSTEM message on purpose: it's invisible in the transcript
# (so it never shows up as a chat bubble), and session_to_chat drops system
# messages, so it can't accumulate -- each device re-derives its own on open.
# mobile/agent-core.js carries a word-for-word copy; keep the two in step.
HANDOFF_MARKER = "[device-handoff]"
# The phone's note about what the desktop's checkout looks like. Re-derived on
# every open like the handoff marker, so it's stripped alongside it.
DESKTOP_STATE_MARKER = "[desktop-state]"

_DEVICE_FACTS = {
    "desktop": "a real machine: you can run commands, tests, servers and a browser here",
    "phone": ("the phone: there is no shell here, so you cannot run commands, tests or "
              "servers -- note anything that needs running and it will happen on the desktop"),
}


# The phone can read and edit the repo but cannot run a single thing. Today it
# says "run the tests when you're back at your desktop" in prose, and that
# intent dies in the transcript -- you arrive at the machine that CAN run them
# with no idea anything is waiting. So the phone records it structurally
# instead, the chat carries it across, and the desktop opens already knowing.
#
# One-directional on purpose: only the phone is missing a capability the other
# device has. session_to_chat writes an empty list unless told otherwise, so
# the desktop working in a chat naturally clears what it was handed.
PENDING_MARKER = "[from-your-phone]"


def pending_note(items: list) -> str:
    """The note handed to the desktop agent for work the phone couldn't do."""
    lines = []
    for i, it in enumerate(items or [], 1):
        task = str(it.get("task", "")).strip()
        if not task:
            continue
        why = str(it.get("why", "")).strip()
        lines.append(f"{i}. {task}" + (f" -- {why}" if why else ""))
    if not lines:
        return ""
    return (
        f"{PENDING_MARKER} Earlier turns of this chat ran on the phone, which has no shell. "
        f"These were left for this machine, which does:\n" + "\n".join(lines) +
        "\nPick them up if they still make sense -- check the code first, since it may have "
        "moved on since. If one no longer applies, say so instead of doing it anyway."
    )


def handoff_note(from_device: str, to_device: str) -> str:
    """The marker spliced in when a chat moves between devices."""
    frm = (from_device or "another device").lower()
    to = (to_device or "").lower()
    return (
        f"{HANDOFF_MARKER} This conversation just moved from the {frm} to the {to}. "
        f"You are now on {_DEVICE_FACTS.get(to, to)}. "
        f"Everything above ran on the {frm}, which has a different set of tools, so earlier "
        f"turns may show tool calls that do not exist here. Do not copy them: only the tools "
        f"offered to you now are real. If you need something this device cannot do, say so "
        f"plainly instead of calling a tool that isn't there."
    )


def apply_handoff(messages: list, from_device: str, to_device: str) -> list:
    """Strip any stale handoff markers, then add one if the device changed."""
    stale = (HANDOFF_MARKER, DESKTOP_STATE_MARKER, PENDING_MARKER)
    out = [m for m in messages
           if not (m.get("role") == "system"
                   and str(m.get("content", "")).startswith(stale))]
    if from_device and to_device and from_device.lower() != to_device.lower():
        out.append({"role": "system", "content": handoff_note(from_device, to_device)})
    return out


# How long a chat marked interrupted is left alone before this machine offers to
# finish it.
#
# The phone resumes its own turn the moment it comes back to the foreground, and
# two devices running the same turn is the one outcome worse than the turn not
# finishing: they would both call the model, both commit, and each would write a
# history the other has never seen. The grace period is what makes "the phone
# came back" the normal case and "the desktop stepped in" the exception.
#
# Measured from the chat's `updated` stamp, which the phone refreshes when it
# saves on being backgrounded -- so the clock starts when the phone went away,
# not when the turn began.
PICKUP_GRACE_MS = 120_000


def pickup_candidates(rows: list, now_ms: int | None = None,
                      grace_ms: int = PICKUP_GRACE_MS) -> list:
    """Chats a phone abandoned mid-turn that this machine could finish.

    Takes index rows (cheap: one file) and returns the ones worth loading in
    full. The index is a cache rebuilt on every save, so a row is a shortlist
    entry and never the last word -- the caller re-reads the chat body and
    checks the flag again before acting on it.

    Deliberately excludes chats this machine last wrote. A desktop turn that
    died is the desktop's own business and it has the session on disk; picking
    it up from here would race the local copy instead of a remote one.
    """
    now = _now_ms() if now_ms is None else now_ms
    out = []
    for row in rows or []:
        if not row.get("interrupted"):
            continue
        if (row.get("device") or "").lower() == "desktop":
            continue
        updated = row.get("updated") or 0
        # A chat saved seconds ago is a phone that is probably still holding
        # it. Also skips rows with no stamp at all rather than treating a
        # missing one as infinitely old.
        if not updated or now - updated < grace_ms:
            continue
        out.append(row)
    # Oldest first: the one that has been waiting longest is the one whose
    # owner has most obviously stopped waiting for it.
    return sorted(out, key=lambda r: r.get("updated") or 0)


INTERRUPTED_TOOL = (
    "ERROR: interrupted before this finished — the app was suspended by the "
    "operating system mid-call. Assume it did not take effect. Do it again if "
    "it is still needed."
)


def heal_interrupted_turn(messages: list) -> list:
    """Make a history that was cut off mid-turn safe to send again.

    The phone records a tool's result immediately after running it, so a turn
    killed between those two points leaves an assistant message whose
    tool_calls have no matching reply. That is not untidy, it is unsendable:
    OpenAI-compatible APIs require every tool_call to be answered and reject
    the whole request otherwise. Adopting such a chat here without repair means
    the first turn on this machine fails, which looks like the desktop being
    broken rather than the phone having been suspended.

    The twin of healInterruptedTurn in mobile/agent-core.js, and it has to stay
    one: both ends adopt the other's histories.

    Gaps are filled rather than the assistant message dropped -- dropping it
    would lose what the model had decided to do -- and the reply says to assume
    the call did not take effect, because a tool that ran without its result
    being recorded is indistinguishable from one that never ran.
    """
    answered = {m.get("tool_call_id") for m in (messages or [])
                if isinstance(m, dict) and m.get("role") == "tool"}
    out: list = []
    for m in messages or []:
        out.append(m)
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for call in (m.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            cid = call.get("id")
            if not cid or cid in answered:
                continue
            # Directly after the message that made the call: the pairing the
            # API checks is positional, so a reply appended at the end of the
            # list is still a rejected request.
            out.append({"role": "tool", "tool_call_id": cid,
                        "content": INTERRUPTED_TOOL})
            answered.add(cid)
    return out


def pickup_note() -> str:
    """The message this machine sends to finish a turn the phone could not.

    A real message rather than a silent continuation, and deliberately so: the
    desktop is about to run tools and possibly commit, without anyone having
    asked it to just now. Something has to say why, in the transcript, where
    both devices will see it.

    It also says what the phone cannot know: that the interruption was the
    operating system, not the model, so there is nothing to diagnose and no
    reason to start over.
    """
    return (
        "Your phone was suspended by its operating system part-way through "
        "this turn, so the request never came back and the turn stopped. "
        "Nothing is wrong with the work itself. Carry on from the last "
        "completed step above -- do not start again from the beginning, and "
        "do not repeat anything that already succeeded."
    )


def session_to_chat(sess: dict, repo_state: dict | None = None,
                    pending: list | None = None, repo: dict | None = None,
                    interrupted: bool = False) -> dict:
    """A desktop SessionStore record -> a sync chat object the phone can read.

    A leading system slot is included at index 0 because the phone overwrites
    messages[0] with its own system prompt on resume; without it, the phone
    would clobber the first real message.

    `repo_state` (optional) is this machine's git state for the project. The
    phone reads the repo over the GitHub API, so it cannot see work that is
    only on the desktop's disk -- it would edit an older copy of a file and
    commit over it. Publishing the state is what lets the phone warn instead."""
    body = [m for m in (sess.get("messages") or []) if m.get("role") != "system"]
    messages = [{"role": "system", "content": ""}] + body
    transcript = _messages_to_transcript(body)
    cwd = sess.get("cwd", "")
    return {
        "id": sess["id"],
        "title": sess.get("title") or "Untitled",
        "preview": (transcript[-1]["text"][:80] if transcript else ""),
        # Which project this chat belongs to, as a short label. All chats now
        # share one repo, so this is what tells them apart in the lists.
        "project": project_label(cwd),
        # Which GitHub repository this chat is actually about, when the folder
        # has a GitHub origin. The phone works only through the GitHub API, so
        # without this it has nothing to check the conversation against -- and
        # it used to fall back to whatever repo happened to be open on the
        # phone, binding a desktop chat to an unrelated codebase and letting
        # the agent edit it. Absent (not guessed) when the folder has no
        # GitHub remote; the phone refuses rather than picking for you.
        "repo": dict(repo) if repo else None,
        "device": "desktop",
        "messages": messages,
        "transcript": transcript,
        # What this machine's checkout looks like right now, so the phone can
        # tell whether GitHub is actually the latest word on this project.
        "repo_state": dict(repo_state) if repo_state else {},
        # Work the phone couldn't run. Defaults to empty, so the desktop
        # pushing after a turn clears whatever it was handed.
        "pending": list(pending or []),
        # Whether a turn is still owed on this chat. Sent explicitly rather
        # than omitted, because absent means "nothing to say" to the merge in
        # save() -- so leaving it out would let a phone's stale True survive
        # the very turn that answered it, and the desktop would pick the chat
        # up again on its next scan, forever.
        "interrupted": bool(interrupted),
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
    # Repaired on the way in, so every route that adopts someone else's chat is
    # covered by one call -- the pickup scan, and the manual pull button, which
    # has always been able to land a phone history killed mid-tool and then
    # fail on its first request.
    messages = heal_interrupted_turn(messages)
    extra = chat.get("desktop") or {}
    return {
        "id": chat.get("id", ""),
        "title": chat.get("title") or "Untitled",
        "messages": messages,
        "cwd": extra.get("cwd", ""),
        "todos": extra.get("todos") or [],
        "model_provider": extra.get("model_provider", ""),
        "model": extra.get("model", ""),
        # Which device last wrote this chat, so the opener can tell whether it
        # is picking up its own work or taking a handoff.
        "device": chat.get("device", ""),
        # Work the phone left for a machine with a shell.
        "pending": list(chat.get("pending") or []),
        # A turn the writing device started and never finished.
        "interrupted": bool(chat.get("interrupted")),
    }
