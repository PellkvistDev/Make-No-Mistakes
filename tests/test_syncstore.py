"""Tests for the desktop half of cross-device session sync (glmcode.syncstore).

Two layers, mirroring the phone's Node tests:

  * Store/branch logic runs anywhere -- a fake GitHub (in-memory) plus a
    key-sensitive fake codec (monkeypatched over aes_encrypt/aes_decrypt), so
    the whole flow is exercised even in the CI env that installs only
    `requests pytest` (no `cryptography`).
  * The real AES-GCM roundtrip and the phone-interop vector run only where the
    native crypto backend is healthy (skipped otherwise, like the segno suite).

The interop vector was produced by the phone's exact primitives (WebCrypto
PBKDF2 + AES-GCM) in Node; decrypting it here proves the two implementations
speak the same format.
"""

import base64
import hashlib
import io
import json
import sys
import threading
import time
import types
import urllib.error

import pytest

from glmcode import githubsync
from glmcode import syncstore
from glmcode.githubsync import GitHubError
from glmcode.syncstore import STATE_BRANCH, SYNC_CHECK, StateRepo, SyncStore, open_sync

needs_crypto = pytest.mark.skipif(
    not syncstore.crypto_available(), reason="cryptography AES-GCM unavailable")


# ------------------------------------------------------------ WebCrypto vector

# Generated in Node with the phone's own primitives (see module docstring).
VECTOR = {
    "passphrase": "correct horse battery staple",
    "salt_b64": "AQgPFh0kKzI5QEdOVVxjag==",
    "key_b64": "oHusj+XCkjWMpcz8/DDotF+kTRoe5DGL0XmlsQL3vNA=",
    "iv_b64": "Aw4ZJC86RVBbZnF8",
    "ct_b64": ("GhjlJK9VWRuf/2BLgbn00K6TXGAIbGPKdVLVAmzFQMtIeQDNsTyPMNsImMCZ"
               "Fo/uU32uL8tq5qWvD4EWB5YPd9QUeureQBq9C7EyL+C8lCNVrxOKKgWu1UTB"
               "q1GyvnVr/pxf9i51CrKiZOYu"),
    "obj": {"v": 1, "chats": [
        {"id": "c1", "title": "Héllo, 世界", "updated": 1700000000000, "preview": "x"}]},
}


def test_derive_key_matches_webcrypto_vector():
    """PBKDF2 is stdlib, so this interop check runs even without cryptography."""
    salt = base64.b64decode(VECTOR["salt_b64"])
    key = syncstore.derive_key(VECTOR["passphrase"], salt)
    assert base64.b64encode(key).decode() == VECTOR["key_b64"]


def test_derive_key_rejects_short_passphrase():
    with pytest.raises(syncstore.SyncError):
        syncstore.derive_key("short", b"0123456789abcdef")


@needs_crypto
def test_decrypts_a_blob_the_phone_produced():
    """The whole point: a blob encrypted by WebCrypto decrypts here byte-perfect."""
    key = base64.b64decode(VECTOR["key_b64"])
    blob = {"v": 1, "iv": VECTOR["iv_b64"], "ct": VECTOR["ct_b64"]}
    assert syncstore.aes_decrypt(blob, key) == VECTOR["obj"]


@needs_crypto
def test_aes_roundtrip_and_no_plaintext_at_rest():
    key = syncstore.derive_key("a real passphrase", b"0123456789abcdef")
    secret = {"title": "Secret Project", "note": "launch codes 0000"}
    blob = syncstore.aes_encrypt(secret, key)
    assert blob["v"] == 1 and blob["iv"] and blob["ct"]
    serialized = json.dumps(blob)
    assert "Secret Project" not in serialized
    assert "launch codes" not in serialized
    assert syncstore.aes_decrypt(blob, key) == secret


@needs_crypto
def test_wrong_key_never_returns_garbage():
    key = syncstore.derive_key("the right one", b"0123456789abcdef")
    blob = syncstore.aes_encrypt({"x": 1}, key)
    other = syncstore.derive_key("a different one", b"0123456789abcdef")
    with pytest.raises(syncstore.SyncError):
        syncstore.aes_decrypt(blob, other)


# --------------------------------------------------------- fake GitHub + codec

class FakeGitHub:
    """In-memory stand-in for githubsync._api over one state branch."""

    def __init__(self, has_branch=False):
        self.files: dict[str, str] = {}
        self.branch_exists = has_branch
        self.orphan_created = 0
        self.calls: list[tuple] = []

    def api(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        p = path.split("?", 1)[0]
        if "/contents/" in p:
            fpath = p.split("/contents/", 1)[1]
            if method == "GET":
                if fpath not in self.files:
                    raise GitHubError("Not found")
                text = self.files[fpath]
                return {"content": base64.b64encode(text.encode()).decode(),
                        "sha": "sha-" + hashlib.sha1(text.encode()).hexdigest()[:8]}
            if method == "PUT":
                self.files[fpath] = base64.b64decode(body["content"]).decode()
                self.branch_exists = True
                return {"commit": {"sha": "c"}}
            if method == "DELETE":
                self.files.pop(fpath, None)
                return {}
        if "/git/ref/heads/" in p:
            if not self.branch_exists:
                raise GitHubError("Not found")
            return {"object": {"sha": "branchsha"}}
        if p.endswith("/git/trees"):
            return {"sha": "treesha"}
        if p.endswith("/git/commits"):
            return {"sha": "commitsha"}
        if p.endswith("/git/refs"):
            self.branch_exists = True
            self.orphan_created += 1
            return {}
        raise GitHubError(f"unexpected {method} {p}")


@pytest.fixture
def fake_codec(monkeypatch):
    """A key-sensitive fake for aes_encrypt/aes_decrypt so the store/branch
    logic can be tested without the native crypto backend. A 4-byte digest of
    the key is prefixed, so a wrong key fails exactly like real AES-GCM would."""
    def enc(obj, key):
        raw = json.dumps(obj).encode()
        tag = hashlib.sha256(key).digest()[:4]
        return {"v": 1, "iv": "", "ct": base64.b64encode(tag + raw).decode()}

    def dec(blob, key):
        data = base64.b64decode(blob["ct"])
        if data[:4] != hashlib.sha256(key).digest()[:4]:
            raise syncstore.SyncError("wrong key")
        return json.loads(data[4:].decode())

    monkeypatch.setattr(syncstore, "aes_encrypt", enc)
    monkeypatch.setattr(syncstore, "aes_decrypt", dec)
    return None


def _repo(fake):
    return StateRepo("TOKEN", "owner", "repo", api=fake.api)


# ------------------------------------------------------------- store behaviour

def test_open_sync_bootstraps_new_store(fake_codec):
    fake = FakeGitHub(has_branch=False)
    key, store, created = open_sync(_repo(fake), "correct horse battery")
    assert created is True
    assert fake.orphan_created == 1, "orphan branch created on first device"
    assert "sync.json" in fake.files
    meta = json.loads(fake.files["sync.json"])
    assert meta["v"] == 1 and meta["salt"] and meta["check"]
    assert isinstance(store, SyncStore)


def test_second_device_right_passphrase_re_derives_key(fake_codec):
    fake = FakeGitHub()
    _k, store, _c = open_sync(_repo(fake), "shared secret 1")
    store.save({"id": "c1", "title": "Hello",
                "messages": [{"role": "user", "content": "hi"}]})
    # second device: same files, fresh open
    _k2, store2, created = open_sync(_repo(fake), "shared secret 1")
    assert created is False
    loaded = store2.load("c1")
    assert loaded["title"] == "Hello"
    assert loaded["messages"][0]["content"] == "hi"


def test_wrong_passphrase_is_rejected(fake_codec):
    fake = FakeGitHub()
    open_sync(_repo(fake), "the real passphrase")
    with pytest.raises(syncstore.SyncError, match="Wrong sync passphrase"):
        open_sync(_repo(fake), "an impostor phrase")


def test_open_sync_rejects_short_passphrase(fake_codec):
    fake = FakeGitHub()
    with pytest.raises(syncstore.SyncError, match="at least 6"):
        open_sync(_repo(fake), "short")


def test_save_list_load_remove_lifecycle(fake_codec):
    fake = FakeGitHub()
    _k, store, _c = open_sync(_repo(fake), "lifecycle pass")
    store.save({"id": "a", "title": "Alpha", "messages": []})
    time.sleep(0.002)  # ensure a strictly later 'updated' timestamp
    store.save({"id": "b", "title": "Beta", "messages": []})

    listed = store.list()
    assert len(listed) == 2
    assert listed[0]["id"] == "b", "newest chat first"
    assert listed[0]["updated"] >= listed[1]["updated"]

    # re-saving updates in place, not duplicates
    store.save({"id": "a", "title": "Alpha renamed", "messages": []})
    listed2 = store.list()
    assert len(listed2) == 2
    assert next(c for c in listed2 if c["id"] == "a")["title"] == "Alpha renamed"

    store.remove("a")
    listed3 = store.list()
    assert [c["id"] for c in listed3] == ["b"]
    with pytest.raises(syncstore.SyncError):
        store.load("a")


def test_list_empty_store_returns_empty(fake_codec):
    fake = FakeGitHub()
    _k, store, _c = open_sync(_repo(fake), "empty store pass")
    assert store.list() == []


def test_existing_branch_is_not_re_orphaned(fake_codec):
    fake = FakeGitHub(has_branch=True)  # branch already exists, no sync.json yet
    open_sync(_repo(fake), "reuse branch pass")
    assert fake.orphan_created == 0, "must not create an orphan when the branch exists"
    assert "sync.json" in fake.files


# --------------------------------------------------- session <-> chat mapping

def test_session_to_chat_is_phone_compatible():
    sess = {
        "id": "20260101-000000-abcdef",
        "title": "Add dark mode",
        "cwd": "/home/me/proj",
        "todos": [{"text": "do it", "done": False}],
        "model_provider": "zai", "model": "glm-4.7",
        "messages": [
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "user", "content": "add a dark mode toggle"},
            {"role": "assistant", "content": "Done — it persists now."},
        ],
    }
    chat = syncstore.session_to_chat(sess)
    # index 0 must be a system slot the phone can safely overwrite
    assert chat["messages"][0]["role"] == "system"
    # the original system prompt is not leaked into the shared store
    assert chat["messages"][0]["content"] == ""
    assert chat["id"] == sess["id"]
    assert chat["title"] == "Add dark mode"
    assert chat["preview"] == "Done — it persists now."
    # transcript renders on the phone (user + assistant text only)
    assert chat["transcript"] == [
        {"role": "user", "text": "add a dark mode toggle"},
        {"role": "assistant", "text": "Done — it persists now."},
    ]
    # desktop extras are namespaced so the phone ignores them
    assert chat["desktop"]["cwd"] == "/home/me/proj"
    assert chat["desktop"]["model"] == "glm-4.7"


def test_chat_to_session_round_trips_a_desktop_chat():
    sess = {
        "id": "s1", "title": "T", "cwd": "/x", "todos": [],
        "model_provider": "p", "model": "m",
        "messages": [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    }
    chat = syncstore.session_to_chat(sess)
    back = syncstore.chat_to_session(chat)
    assert back["id"] == "s1"
    assert back["cwd"] == "/x"
    assert back["model"] == "m"
    # the system slot is dropped; desktop rebuilds its own system prompt
    assert [m["role"] for m in back["messages"]] == ["user", "assistant"]
    assert back["messages"][0]["content"] == "hi"


def test_chat_to_session_reads_a_phone_written_chat():
    """A chat the phone wrote (no 'desktop' block) still loads cleanly."""
    phone_chat = {
        "id": "p1", "title": "From phone", "preview": "hey",
        "messages": [
            {"role": "system", "content": "phone system prompt"},
            {"role": "user", "content": "hey"},
        ],
        "transcript": [{"role": "user", "text": "hey"}],
    }
    back = syncstore.chat_to_session(phone_chat)
    assert back["id"] == "p1"
    assert back["title"] == "From phone"
    assert back["cwd"] == "" and back["model"] == ""
    assert [m["role"] for m in back["messages"]] == ["user"]


# ------------------------------------------------- real HTTP layer (StateRepo)
# The FakeGitHub above stands in for githubsync._api, so these drive the REAL
# _api through a fake urlopen to pin the request shapes: state branch on every
# call, base64 bodies, and the orphan-commit sequence.

class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _recording_urlopen(handler, log):
    def opener(req, timeout=0):
        body = json.loads(req.data.decode()) if req.data else None
        log.append((req.get_method(), req.full_url, body))
        return _Resp(json.dumps(handler(req.get_method(), req.full_url, body)).encode())
    return opener


def test_state_repo_targets_the_state_branch_over_real_api(monkeypatch):
    log = []

    def handler(method, url, body):
        if method == "GET" and "/contents/" in url:
            return {"content": base64.b64encode(b'{"hello": 1}').decode(), "sha": "s1"}
        return {"commit": {"sha": "c"}}

    monkeypatch.setattr(githubsync.urllib.request, "urlopen",
                        _recording_urlopen(handler, log))
    repo = StateRepo("TOKEN", "own", "rep")

    text, sha = repo.get_file("index.json")
    assert text == '{"hello": 1}' and sha == "s1"
    method, url, _ = log[-1]
    assert url.endswith(f"/repos/own/rep/contents/index.json?ref={STATE_BRANCH}")

    repo.put_file("chats/x.json", "PAYLOAD", "msg", "oldsha")
    method, url, body = log[-1]
    assert method == "PUT" and url.endswith("/repos/own/rep/contents/chats/x.json")
    assert base64.b64decode(body["content"]).decode() == "PAYLOAD"
    assert body["branch"] == STATE_BRANCH and body["sha"] == "oldsha"

    repo.delete_file("chats/x.json", "gone", "sha9")
    method, url, body = log[-1]
    assert method == "DELETE" and body["branch"] == STATE_BRANCH


def test_branch_sha_returns_none_when_branch_is_absent(monkeypatch):
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(githubsync.urllib.request, "urlopen", opener)
    assert StateRepo("T", "o", "r").branch_sha() is None


def test_create_orphan_branch_commits_with_no_parents(monkeypatch):
    log = []

    def handler(method, url, body):
        if url.endswith("/git/trees"):
            return {"sha": "TREE"}
        if url.endswith("/git/commits"):
            return {"sha": "COMMIT"}
        return {}

    monkeypatch.setattr(githubsync.urllib.request, "urlopen",
                        _recording_urlopen(handler, log))
    assert StateRepo("T", "o", "r").create_orphan_branch() == "COMMIT"

    trees = next(b for m, u, b in log if u.endswith("/git/trees"))
    assert trees["tree"][0]["path"] == ".mnm"
    commits = next(b for m, u, b in log if u.endswith("/git/commits"))
    assert commits["parents"] == [], "orphan: no parents, so no code history"
    assert commits["tree"] == "TREE"
    refs = next(b for m, u, b in log if u.endswith("/git/refs"))
    assert refs["ref"] == f"refs/heads/{STATE_BRANCH}" and refs["sha"] == "COMMIT"


# ----------------------------------------------------- GUI API sync endpoints
# The Api object is heavy to construct, so these build a bare instance and
# stub just the collaborators each endpoint touches -- enough to pin the
# routing and the error paths the UI depends on.

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402


class _FakeStore:
    """Stands in for the local SessionStore."""

    def __init__(self, rows=(), data=None):
        self._rows = list(rows)
        self._data = data or {}

    def list(self):
        return self._rows

    def load(self, sid):
        return self._data.get(sid)


def _bare_api(active=None, **attrs):
    """A bare Api with just the collaborators the sync endpoints touch.
    `_active` is a read-only property over _chats[session_id], so an active
    chat is injected by populating those instead of assigning to it."""
    api = object.__new__(gui_app.Api)
    api._chats = {}
    api.session_id = ""
    api._store = _FakeStore()
    for k, v in attrs.items():
        setattr(api, k, v)
    if active is not None:
        api.session_id = api.session_id or "active-sid"
        api._chats[api.session_id] = active
    return api


def test_sync_works_without_a_connected_repo(monkeypatch):
    """The whole point of the central store: chats sync even when the project
    they belong to isn't a GitHub repo at all."""
    class Store:
        def list(self):
            return [{"id": "a", "title": "A", "updated": 1}]

    api = _bare_api()
    monkeypatch.setattr(gui_app.Api, "_active_repo_coords", lambda self: None)
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: "T")
    monkeypatch.setattr(gui_app.syncstore, "open_central",
                        lambda passphrase=None, token=None, api=None: (b"k", Store(), False))
    res = api.sync_list_chats()
    assert "error" not in res
    assert [c["id"] for c in res["chats"]] == ["a"]


def test_sync_set_passphrase_rejects_short_and_reports_verify_failure(monkeypatch):
    api = _bare_api()
    assert "at least 6" in api.sync_set_passphrase("abc")["error"]

    monkeypatch.setattr(gui_app.syncstore, "crypto_status", lambda: ("ok", ""))
    monkeypatch.setattr(gui_app.Api, "_active_repo_coords",
                        lambda self: ("github.com", "o", "r", None))
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: "T")

    def boom(passphrase=None, token=None, api=None):
        raise syncstore.SyncError("Wrong sync passphrase.")

    saved = []
    monkeypatch.setattr(gui_app.syncstore, "open_central", boom)
    monkeypatch.setattr(gui_app.syncstore, "save_passphrase", lambda p: saved.append(p))
    res = api.sync_set_passphrase("a good passphrase")
    assert res["error"] == "Wrong sync passphrase."
    assert saved == [], "a passphrase that fails verification must NOT be stored"


def test_sync_list_chats_flags_which_chats_are_already_local(monkeypatch):
    class Store:
        def list(self):
            return [{"id": "a", "title": "A", "updated": 2},
                    {"id": "b", "title": "B", "updated": 1}]

    api = _bare_api(_store=_FakeStore(rows=[{"id": "a"}]))
    monkeypatch.setattr(gui_app.Api, "_open_sync_store", lambda self: (Store(), None))
    rows = api.sync_list_chats()["chats"]
    assert {r["id"]: r["local"] for r in rows} == {"a": True, "b": False}


def test_sync_pull_chat_lands_a_phone_chat_in_a_usable_folder(monkeypatch, tmp_path):
    """A phone-written chat has no cwd (and another machine's cwd won't exist
    here), so it must open in the active workdir rather than a dead path."""
    phone_chat = {
        "id": "p1", "title": "From phone",
        "messages": [{"role": "system", "content": ""},
                     {"role": "user", "content": "hey"}],
    }

    class Store:
        def load(self, cid):
            assert cid == "p1"
            return phone_chat

    active = types.SimpleNamespace(agent=types.SimpleNamespace(workdir=tmp_path))
    api = _bare_api(active=active)
    monkeypatch.setattr(gui_app.Api, "_open_sync_store", lambda self: (Store(), None))
    monkeypatch.setattr(gui_app.Api, "_save_current", lambda self: None)
    monkeypatch.setattr(gui_app.Api, "list_sessions", lambda self: [])

    seen = {}

    def fake_activate(self, sid, messages, cwd, pt, ct, todos, title="", **kw):
        seen.update(sid=sid, messages=messages, cwd=cwd, title=title)
        return {"ok": True}

    monkeypatch.setattr(gui_app.Api, "_activate_session", fake_activate)
    res = api.sync_pull_chat("p1")
    assert "error" not in res
    assert seen["sid"] == "p1" and seen["title"] == "From phone"
    assert seen["cwd"] == str(tmp_path), "must fall back to the active workdir"
    # the phone's empty system slot is dropped before activation
    assert [m["role"] for m in seen["messages"]] == ["user"]


def test_sync_push_chat_uploads_the_saved_session(monkeypatch):
    sess = {"id": "s1", "title": "T", "cwd": "/x",
            "messages": [{"role": "system", "content": "SYS"},
                         {"role": "user", "content": "hi"}]}
    uploaded = []

    class Store:
        def save(self, chat):
            uploaded.append(chat)
            return 123

    api = _bare_api(_store=_FakeStore(data={"s1": sess}), session_id="s1")
    monkeypatch.setattr(gui_app.Api, "_open_sync_store", lambda self: (Store(), None))
    res = api.sync_push_chat("")
    assert res["ok"] is True and res["id"] == "s1"
    assert uploaded[0]["id"] == "s1"
    # the local system prompt is never shared, but the slot is kept for the phone
    assert uploaded[0]["messages"][0] == {"role": "system", "content": ""}
    assert uploaded[0]["transcript"] == [{"role": "user", "text": "hi"}]


# ------------------------------------------- crypto availability diagnostics
# `cryptography` only became a listed requirement when sync shipped, so an
# install that predates it reports sync as unavailable. That must say WHY and
# how to fix it -- these pin the wording contract the settings panel relies on.

def test_crypto_status_missing_names_the_package_and_the_fix(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_cryptography(name, *a, **kw):
        if name.startswith("cryptography"):
            raise ImportError("No module named 'cryptography'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_cryptography)
    state, why = syncstore.crypto_status()
    assert state == "missing"
    assert "cryptography" in why
    assert syncstore.INSTALL_HINT in why, "must hand the user the exact command"
    assert "install.ps1" in why
    assert not syncstore.crypto_available()


def test_crypto_status_broken_backend_is_distinguished_from_missing(monkeypatch):
    """A broken native build raises a BaseException (Rust PanicException), not
    ImportError -- it needs a reinstall, not a first install."""
    import builtins
    real_import = builtins.__import__

    class Panic(BaseException):
        pass

    def boom(name, *a, **kw):
        if name.startswith("cryptography"):
            raise Panic("Python API call failed")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    state, why = syncstore.crypto_status()
    assert state == "broken"
    assert "Panic" in why and "force-reinstall" in why
    assert not syncstore.crypto_available()


def test_sync_env_surfaces_the_reason_and_command(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.Api, "_active_repo_coords", lambda self: None)
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: None)
    monkeypatch.setattr(gui_app.syncstore, "crypto_status",
                        lambda: ("missing", "needs cryptography: pip install it"))
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase", lambda: None)
    env = api.sync_env()
    assert env["available"] is False
    assert env["crypto_state"] == "missing"
    assert env["crypto_reason"] == "needs cryptography: pip install it"
    assert env["install_hint"] == syncstore.INSTALL_HINT


def test_sync_set_passphrase_reports_the_real_crypto_reason(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.syncstore, "crypto_status",
                        lambda: ("missing", "Chat sync needs the 'cryptography' package"))
    res = api.sync_set_passphrase("a long enough passphrase")
    assert res["error"] == "Chat sync needs the 'cryptography' package"


# ------------------------------------------------- the dedicated sync repo
# One private repo holds every chat, created on demand. This is what lets the
# UI be a single list with nothing to pick.

class FakeAccount(FakeGitHub):
    """FakeGitHub plus /user and repo create/exists, for ensure_sync_repo."""

    def __init__(self, repo_exists=False, login="octo", create_fails=False):
        super().__init__(has_branch=repo_exists)
        self.login = login
        self.repo_exists = repo_exists
        self.create_fails = create_fails
        self.created = []

    def api(self, method, path, token, body=None):
        p = path.split("?", 1)[0]
        if p == "/user":
            return {"login": self.login}
        if p == f"/repos/{self.login}/{syncstore.SYNC_REPO_NAME}" and method == "GET":
            if not self.repo_exists:
                raise GitHubError("Not Found")
            return {"full_name": f"{self.login}/{syncstore.SYNC_REPO_NAME}"}
        if p == "/user/repos" and method == "POST":
            if self.create_fails:
                raise GitHubError("Resource not accessible by personal access token")
            self.created.append(body)
            self.repo_exists = True
            self.branch_exists = True
            return {"full_name": f"{self.login}/{body['name']}"}
        return super().api(method, path, token, body)


def test_ensure_sync_repo_creates_a_private_repo_once():
    fake = FakeAccount(repo_exists=False)
    owner, name = syncstore.ensure_sync_repo("T", api=fake.api)
    assert (owner, name) == ("octo", syncstore.SYNC_REPO_NAME)
    assert len(fake.created) == 1
    body = fake.created[0]
    assert body["private"] is True, "chat history must never land in a public repo"
    assert body["auto_init"] is True, "auto_init so main exists to write to"

    # second call finds it and does NOT create another
    owner2, name2 = syncstore.ensure_sync_repo("T", api=fake.api)
    assert (owner2, name2) == (owner, name)
    assert len(fake.created) == 1


def test_ensure_sync_repo_explains_a_permission_failure():
    fake = FakeAccount(repo_exists=False, create_fails=True)
    with pytest.raises(syncstore.SyncError, match="Administration"):
        syncstore.ensure_sync_repo("T", api=fake.api)


def test_open_central_uses_the_dedicated_repo(fake_codec, monkeypatch):
    fake = FakeAccount(repo_exists=False)
    monkeypatch.setattr(syncstore, "crypto_status", lambda: ("ok", ""))
    key, store, created = syncstore.open_central("a good passphrase", token="T",
                                                 api=fake.api)
    assert created is True
    assert store.repo.repo == syncstore.SYNC_REPO_NAME
    assert store.repo.branch == syncstore.SYNC_REPO_BRANCH, "writes go to main"
    store.save({"id": "x", "title": "T", "messages": []})
    assert "chats/x.json" in fake.files


def test_index_entries_carry_project_and_device(fake_codec):
    """The sidebar shows which project a chat came from and which device wrote
    it -- those live in the index so listing doesn't need to fetch every chat."""
    fake = FakeGitHub()
    _k, store, _c = open_sync(_repo(fake), "listing pass")
    store.save({"id": "c1", "title": "T", "project": "my-app",
                "device": "phone", "messages": []})
    row = store.list()[0]
    assert row["project"] == "my-app"
    assert row["device"] == "phone"


def test_project_label_is_the_folder_name():
    assert syncstore.project_label(r"C:\Users\me\code\my-app") == "my-app"
    assert syncstore.project_label("/home/me/code/my-app/") == "my-app"
    assert syncstore.project_label("") == ""


def test_session_to_chat_tags_project_and_device():
    chat = syncstore.session_to_chat({
        "id": "s1", "title": "T", "cwd": "/home/me/proj/widgets",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert chat["project"] == "widgets"
    assert chat["device"] == "desktop"


def test_autosync_is_skipped_without_a_passphrase(monkeypatch):
    """No passphrase = sync off; the background push must not fire at all."""
    api = _bare_api()
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase", lambda: None)
    pushed = []
    monkeypatch.setattr(gui_app.Api, "sync_push_chat",
                        lambda self, sid="": pushed.append(sid))
    api._maybe_autosync("s1")
    assert pushed == []


def test_autosync_pushes_in_the_background(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase", lambda: "pass")
    done = threading.Event()
    pushed = []

    def fake_push(self, sid=""):
        pushed.append(sid)
        done.set()

    monkeypatch.setattr(gui_app.Api, "sync_push_chat", fake_push)
    api._maybe_autosync("s1")
    assert done.wait(2), "auto-sync should push on a background thread"
    assert pushed == ["s1"]


def test_autosync_swallows_failures(monkeypatch):
    """A failed push must never surface: the local session is already saved and
    the next turn retries."""
    api = _bare_api()
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase", lambda: "pass")
    done = threading.Event()

    def boom(self, sid=""):
        done.set()
        raise RuntimeError("offline")

    monkeypatch.setattr(gui_app.Api, "sync_push_chat", boom)
    api._maybe_autosync("s1")          # must not raise
    assert done.wait(2)


# ------------------------------------------- start a chat from a new GitHub repo
# "New chat" can now create a repo and open it directly, so the create call has
# to produce a repo that is actually clonable (an empty one has no branch).

def test_create_repo_can_auto_init(monkeypatch):
    sent = {}

    def fake_api(method, path, token, body=None):
        sent.update(method=method, path=path, body=body)
        return {"full_name": "octo/thing", "owner": {"login": "octo"},
                "name": "thing", "default_branch": "main"}

    monkeypatch.setattr(githubsync, "_api", fake_api)
    made = githubsync.create_repo("T", "thing", private=True, auto_init=True)
    assert sent["body"]["auto_init"] is True, "a repo we clone needs an initial commit"
    assert sent["body"]["private"] is True
    assert made["full_name"] == "octo/thing"

    githubsync.create_repo("T", "thing")          # default stays as it was
    assert sent["body"]["auto_init"] is False


def test_github_create_and_open_creates_then_clones(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: "T")
    monkeypatch.setattr(gui_app.githubsync, "available", lambda: True)
    made = {}

    def fake_create(token, name, private=True, description="", auto_init=False):
        made.update(name=name, private=private, auto_init=auto_init)
        return {"full_name": f"octo/{name}", "owner": "octo", "name": name,
                "default_branch": "main"}

    cloned = {}
    monkeypatch.setattr(gui_app.githubsync, "create_repo", fake_create)
    monkeypatch.setattr(gui_app.Api, "github_clone",
                        lambda self, url, auto_backup=True: cloned.update(
                            url=url, auto_backup=auto_backup) or {"ok": True})

    res = api.github_create_and_open("widgets", True, False)
    assert res == {"ok": True}
    assert made == {"name": "widgets", "private": True, "auto_init": True}
    assert cloned == {"url": "octo/widgets", "auto_backup": False}


def test_github_create_and_open_explains_a_permission_failure(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: "T")
    monkeypatch.setattr(gui_app.githubsync, "available", lambda: True)

    def boom(*a, **kw):
        raise gui_app.githubsync.GitHubError("Resource not accessible by personal access token")

    monkeypatch.setattr(gui_app.githubsync, "create_repo", boom)
    err = api.github_create_and_open("widgets")["error"]
    assert "Administration: Read and write" in err


def test_github_create_and_open_needs_a_name_and_token(monkeypatch):
    api = _bare_api()
    monkeypatch.setattr(gui_app.githubsync, "available", lambda: True)
    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: None)
    assert "token" in api.github_create_and_open("x")["error"]

    monkeypatch.setattr(gui_app.Api, "_gh_token", lambda self: "T")
    assert "Name the new repository" in api.github_create_and_open("  ")["error"]
