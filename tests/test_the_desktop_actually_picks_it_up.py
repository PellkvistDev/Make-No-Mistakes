"""The chain from "the phone was suspended" to "this machine finished it".

Every piece of this had a unit test. The seam between them had none -- there
were no tests of `sync_finish_interrupted` at all, despite a comment in
`_finish_one_interrupted` referring to "the pickup tests" -- so this drives the
real store, the real shortlist and the real re-read, and the parts have to
agree with each other.

What it does NOT cover, and cannot: the bug that had this feature switched off
for every chat was on the PHONE's side of the store. It wrote `interrupted`
into the chat body and built an index row without it, and `pickup_candidates`
reads rows. Both halves are here in the desktop's own code and both were
right; what disagreed was the other program writing the same file. That is
what the node parity test in tests/test_the_index_is_a_cache.py is for, and
saying so is the point -- an integration test that stops at the language
boundary should not be described as covering what is on the other side of it.

The one thing about that which IS assertable from here is the consequence, and
it is below: a row without the flag is never picked up, so a phone still
running an older build simply does not get finished for. The row is a cache
and re-reading every body per tick would cost a decrypt per chat, so this is
the trade rather than an oversight.
"""

import base64
import hashlib
import threading
import types

import pytest

from glmcode.githubsync import GitHubError

sys_modules_stub = types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object())
import sys
sys.modules.setdefault("webview", sys_modules_stub)

from glmcode import config as config_mod          # noqa: E402
from glmcode import syncstore                     # noqa: E402
from glmcode.gui import app as gui_app            # noqa: E402

needs_crypto = pytest.mark.skipif(not syncstore.crypto_available(),
                                  reason="cryptography unavailable")

PASS = "correct-horse-battery-staple"
LONG_AGO = 1_000_000
NOW = LONG_AGO + syncstore.PICKUP_GRACE_MS * 10


class FakeGitHub:
    def __init__(self):
        self.files: dict[str, str] = {}

    def api(self, method, path, token, body=None):
        p = path.split("?", 1)[0]
        if "/contents/" in p:
            f = p.split("/contents/", 1)[1]
            if method == "GET":
                if f not in self.files:
                    raise GitHubError("Not found", 404)
                t = self.files[f]
                return {"content": base64.b64encode(t.encode()).decode(),
                        "sha": "sha-" + hashlib.sha1(t.encode()).hexdigest()[:8]}
            if method == "PUT":
                self.files[f] = base64.b64decode(body["content"]).decode()
                return {"commit": {"sha": "c"}}
            if method == "DELETE":
                self.files.pop(f, None)
                return {}
        if "/git/ref/heads/" in p:
            return {"object": {"sha": "b"}}
        raise GitHubError(f"unexpected {method} {p}", 500)


@pytest.fixture
def desktop(monkeypatch):
    """An Api with sync on, nothing running, and the turn stubbed at the edge.

    Everything up to the decision is real: the encrypted store, the index, the
    shortlist and the re-read of the body.
    """
    gh = FakeGitHub()
    repo = syncstore.StateRepo("T", "o", "r", api=gh.api)
    _key, store, _created = syncstore.open_sync(repo, PASS)

    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = config_mod.Config()
    api._cfg.sync_finish_interrupted = True
    api._chats = {}
    api._gh_token = lambda: "T"
    api._open_sync_store = lambda: (store, None)
    api.webpush_keys = lambda: {"public": "P"}
    monkeypatch.setattr(syncstore, "crypto_available", lambda: True)
    monkeypatch.setattr(syncstore, "load_passphrase", lambda *a, **k: PASS)
    monkeypatch.setattr(syncstore, "_now_ms", lambda: NOW)

    api.started = []
    api._finish_one_interrupted = lambda cid, chat: (
        api.started.append((cid, chat)) or {"ok": True, "picked": cid})
    api.store, api.gh = store, gh
    return api


def _phone_saved(store, cid, *, interrupted=True, device="phone", when=LONG_AGO):
    """What the phone leaves behind when iOS kills it mid-turn."""
    chat = {"id": cid, "title": cid, "device": device,
            "messages": [{"role": "system", "content": ""},
                         {"role": "user", "content": "refactor the parser"}],
            "transcript": [], "interrupted": interrupted}
    store.save(chat)
    # save() stamps `updated` with the real clock; the grace period is measured
    # from it, so a chat that is meant to look abandoned has to be aged.
    idx, sha = store._read_index()
    for row in idx["chats"]:
        if row["id"] == cid:
            row["updated"] = when
    store._write_index(idx["chats"], sha, idx.get("deleted"))
    body = store.load(cid)
    body["updated"] = when
    blob = syncstore.aes_encrypt(body, store.key)
    import json
    store.repo.put_file(f"chats/{cid}.json", json.dumps(blob), "age it",
                        store._file_sha(f"chats/{cid}.json"))


# ---- the whole chain ------------------------------------------------------

@needs_crypto
def test_a_turn_the_phone_was_suspended_through_is_picked_up(desktop):
    """The one that never happened. If this fails, the feature is off."""
    _phone_saved(desktop.store, "abandoned")
    res = desktop.sync_finish_interrupted()
    assert res["picked"] == "abandoned", "the desktop never saw it"
    assert desktop.started and desktop.started[0][0] == "abandoned"


@needs_crypto
def test_the_row_is_what_carries_it(desktop):
    """The body knowing is not enough: the shortlist is built from index rows,
    so a row without the flag is a chat that is never even loaded."""
    _phone_saved(desktop.store, "abandoned")
    row = next(r for r in desktop.store.list() if r["id"] == "abandoned")
    assert row["interrupted"] is True
    assert [r["id"] for r in syncstore.pickup_candidates(desktop.store.list())] \
        == ["abandoned"]


@needs_crypto
def test_a_row_written_without_the_flag_is_simply_never_picked_up(desktop):
    """What an older phone build leaves behind, and the cost of the trade.

    The devices update independently, so a row missing the field is a real
    thing to receive rather than a hypothetical. It is skipped: re-reading
    every body on a 30-second timer to find out otherwise would be a decrypt
    per chat per tick.
    """
    _phone_saved(desktop.store, "old-build")
    idx, sha = desktop.store._read_index()
    for row in idx["chats"]:
        row.pop("interrupted", None)
    desktop.store._write_index(idx["chats"], sha, idx.get("deleted"))

    assert desktop.sync_finish_interrupted()["picked"] is None
    assert desktop.store.load("old-build")["interrupted"] is True   # the body knew


# ---- and the things that must stop it -------------------------------------

@needs_crypto
def test_a_chat_this_machine_wrote_is_its_own_business(desktop):
    _phone_saved(desktop.store, "mine", device="desktop")
    assert desktop.sync_finish_interrupted()["picked"] is None


@needs_crypto
def test_a_finished_turn_is_left_alone(desktop):
    _phone_saved(desktop.store, "done", interrupted=False)
    assert desktop.sync_finish_interrupted()["picked"] is None


@needs_crypto
def test_a_phone_that_only_just_went_away_is_given_time(desktop):
    """The phone resumes its own turn the instant it comes back, so the grace
    period is what stops both devices running the same one."""
    _phone_saved(desktop.store, "fresh", when=NOW - 1000)
    assert desktop.sync_finish_interrupted()["picked"] is None


@needs_crypto
def test_the_body_gets_the_last_word(desktop):
    """The index is a cache. Between listing it and acting, the phone may have
    come back and finished the turn itself, so the body is re-read."""
    _phone_saved(desktop.store, "raced")
    body = desktop.store.load("raced")
    body["interrupted"] = False
    import json
    blob = syncstore.aes_encrypt(body, desktop.store.key)
    desktop.store.repo.put_file("chats/raced.json", json.dumps(blob), "phone came back",
                                desktop.store._file_sha("chats/raced.json"))
    assert desktop.sync_finish_interrupted()["picked"] is None
    assert not desktop.started


@needs_crypto
def test_a_chat_already_running_here_is_skipped_before_the_pull(desktop):
    """sync_pull_chat re-activates the session from the store, so pulling over
    a live chat overwrites the messages the running agent is holding. The
    check has to come first."""
    _phone_saved(desktop.store, "busy")
    live = types.SimpleNamespace(turn_lock=threading.Lock())
    live.turn_lock.acquire()
    desktop._chats["busy"] = live
    assert desktop.sync_finish_interrupted()["picked"] is None


@needs_crypto
def test_sync_switched_off_does_nothing_at_all(desktop):
    _phone_saved(desktop.store, "abandoned")
    desktop._cfg.sync_finish_interrupted = False
    assert desktop.sync_finish_interrupted() == {"ok": True, "picked": None}
    assert not desktop.started


@needs_crypto
def test_being_offline_is_a_quiet_no_op(desktop, monkeypatch):
    """It runs on a 30-second timer: a machine with no network must do nothing
    rather than log an error every tick."""
    _phone_saved(desktop.store, "abandoned")

    def offline():
        raise syncstore.SyncError("Couldn't reach GitHub")
    monkeypatch.setattr(desktop.store, "list", offline)
    assert desktop.sync_finish_interrupted() == {"ok": True, "picked": None}
