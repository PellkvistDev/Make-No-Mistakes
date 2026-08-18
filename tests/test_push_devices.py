"""Registering a phone for push, and forgetting one that is gone.

The desktop finishes turns the phone was suspended through. It had no way to
say so -- you found out by opening the app and looking. The subscription is how
it reaches a phone that is not running, and it travels in the encrypted sync
store the two devices already share, so no server is introduced to carry it.

A subscription is not a secret in the "keep it off disk" sense, but it IS a URL
that anyone holding it can push to, plus the keys that encrypt those messages.
It goes in the encrypted store like everything else, never in the index.
"""

import pytest

pytest.importorskip("cryptography", reason="the sync store needs cryptography")

from glmcode import syncstore  # noqa: E402


class _Repo:
    """A StateRepo stand-in: files in a dict, shas that change on write."""

    def __init__(self):
        self.files = {}
        self.writes = 0

    def get_file(self, path):
        if path not in self.files:
            raise syncstore.GitHubError("404")
        return self.files[path], f"sha-{path}-{self.writes}"

    def put_file(self, path, text, message, sha=None):
        self.writes += 1
        self.files[path] = text
        return f"sha-{path}-{self.writes}"


def _store():
    return syncstore.SyncStore(_Repo(), syncstore.derive_key("a-long-enough-passphrase", b"0" * 16))


def _sub(endpoint="https://push.example.com/a", auth="secret-a"):
    return {"endpoint": endpoint,
            "keys": {"p256dh": "BPublicKeyHere", "auth": auth}}


def test_no_devices_registered_is_an_empty_list_not_an_error():
    """Read on every send, and almost always empty. It must not raise."""
    assert _store().push_subscriptions() == []


def test_a_registered_device_comes_back():
    store = _store()
    store.add_push_subscription(_sub())
    assert store.push_subscriptions() == [_sub()]


def test_a_second_phone_is_added_not_replaced():
    """One desktop, more than one phone."""
    store = _store()
    store.add_push_subscription(_sub("https://push.example.com/a"))
    store.add_push_subscription(_sub("https://push.example.com/b"))
    assert len(store.push_subscriptions()) == 2


def test_resubscribing_the_same_device_updates_it():
    """A browser that rotates its keys re-subscribes at the same endpoint.
    Appending would leave a stale row that every later send fails against."""
    store = _store()
    store.add_push_subscription(_sub(auth="old-key"))
    store.add_push_subscription(_sub(auth="new-key"))

    subs = store.push_subscriptions()
    assert len(subs) == 1
    assert subs[0]["keys"]["auth"] == "new-key"


def test_a_gone_device_can_be_forgotten():
    """404/410 from the push service. Retrying forever is the alternative."""
    store = _store()
    store.add_push_subscription(_sub("https://push.example.com/a"))
    store.add_push_subscription(_sub("https://push.example.com/b"))

    store.drop_push_subscription("https://push.example.com/a")

    assert [s["endpoint"] for s in store.push_subscriptions()] == \
        ["https://push.example.com/b"]


def test_forgetting_one_that_was_never_there_is_harmless():
    """Two sends can race on the same dead endpoint."""
    store = _store()
    store.add_push_subscription(_sub())
    store.drop_push_subscription("https://push.example.com/nope")
    assert len(store.push_subscriptions()) == 1


def test_a_subscription_without_an_endpoint_is_refused():
    with pytest.raises(syncstore.SyncError, match="endpoint"):
        _store().add_push_subscription({"keys": {"auth": "x"}})


def test_nothing_is_stored_in_the_clear():
    """The endpoint is a URL anyone holding it can push to, and the keys
    decrypt the messages. GitHub must see neither."""
    store = _store()
    store.add_push_subscription(_sub(auth="the-secret"))

    raw = store.repo.files[syncstore.SyncStore.PUSH_PATH]
    assert "push.example.com" not in raw
    assert "the-secret" not in raw


def test_a_store_with_the_wrong_key_reads_no_devices():
    """Rather than raising into the middle of a send. A wrong key here means
    the passphrase changed, and the phone will re-register."""
    store = _store()
    store.add_push_subscription(_sub())
    other = syncstore.SyncStore(store.repo, syncstore.derive_key("a-different-passphrase", b"0" * 16))
    assert other.push_subscriptions() == []


# --------------------------------------------------- the key that enables it --

def test_no_key_published_yet_reads_as_empty():
    """A phone that pairs before any desktop has published one must be told
    "not yet", not handed a broken subscription."""
    assert _store().vapid_public_key() == ""


def test_the_key_round_trips():
    store = _store()
    store.set_vapid_public_key("BPublicKeyFromTheDesktop")
    assert store.vapid_public_key() == "BPublicKeyFromTheDesktop"


def test_publishing_the_same_key_again_writes_nothing():
    """This runs on the desktop's 30-second timer. A write per tick is a commit
    per tick in the user's own sync repository."""
    store = _store()
    store.set_vapid_public_key("BSameKey")
    writes = store.repo.writes
    store.set_vapid_public_key("BSameKey")
    assert store.repo.writes == writes


def test_a_changed_key_is_published():
    store = _store()
    store.set_vapid_public_key("BOldKey")
    store.set_vapid_public_key("BNewKey")
    assert store.vapid_public_key() == "BNewKey"


def test_the_phone_and_the_desktop_look_in_the_same_places():
    """Both halves are hand-written, in two languages. A mismatch is a phone
    that registers successfully and is never pushed to -- silent on both
    sides, which is the worst shape this bug could take."""
    import pathlib
    import shutil
    import subprocess

    core = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
    if not (shutil.which("node") and core.is_file()):
        pytest.skip("node unavailable")
    out = subprocess.run(
        ["node", "-e",
         "const C=require(process.argv[1]);"
         "console.log(JSON.stringify([C.PUSH_PATH, C.VAPID_PATH]));", str(core)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    import json as _json
    assert _json.loads(out.stdout) == [syncstore.SyncStore.PUSH_PATH,
                                       syncstore.SyncStore.VAPID_PATH]
