""""I could not find out" published as "there is nothing to worry about".

Two places, the same shape as the sync index: a failure answered with the
value that means everything is fine, where fine is a claim the caller then
acts on. Both are about losing work rather than about a wrong pixel.
"""

import json
import types


def _gui_app():
    """Imported through a function because glmcode.gui.app needs the webview
    stub in place first, and these constants are read at module level."""
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.gui import app
    return app

import pytest

from glmcode import githubsync, secretstore, syncstore

# What the app really files these under. Named here so no test below has to
# put a credential-shaped key beside a string.
TOKEN_ACCOUNT = githubsync._account("github.com")
PASS_ACCOUNT = syncstore._pass_account()
VAPID_ACCOUNT = _gui_app().Api.VAPID_ACCOUNT


# ---- the desktop's git state, as the phone reads it ----------------------

def test_a_git_hiccup_stops_telling_the_phone_it_is_safe(monkeypatch):
    """_repo_state answered any exception with {}.

    The phone reads the repo over the GitHub API, so uncommitted work on the
    desktop is invisible to it, and repo_state is the only thing that warns.
    An empty dict produces no warning at all -- and because SyncStore.save
    merges, the empty dict is PRESENT and overwrites the last true answer, so
    one hiccup silences the warning until the next successful read.
    """
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app

    api = gui_app.Api.__new__(gui_app.Api)

    def boom(_path):
        raise OSError("git exploded")
    monkeypatch.setattr(gui_app.githubsync, "status", boom)
    assert api._repo_state("/tmp/proj") is None, "a hiccup claimed everything was fine"

    monkeypatch.setattr(gui_app.githubsync, "status",
                        lambda _p: types.SimpleNamespace(connected=False))
    assert api._repo_state("/tmp/proj") == {}, "a project with no remote IS known-fine"

    monkeypatch.setattr(gui_app.githubsync, "status",
                        lambda _p: types.SimpleNamespace(
                            connected=True, branch="wip", dirty=True, ahead=3))
    got = api._repo_state("/tmp/proj")
    assert got["branch"] == "wip" and got["dirty"] is True and got["ahead"] == 3


def test_an_absent_repo_state_leaves_the_last_true_one_standing():
    """The point of omitting it rather than sending {}: the store merges."""
    keep = {"branch": "wip", "dirty": True, "ahead": 3, "at": 1}
    with_state = syncstore.session_to_chat({"id": "s1", "messages": []}, keep)
    unknown = syncstore.session_to_chat({"id": "s1", "messages": []}, None)
    merged = {**with_state, **unknown}
    assert merged["repo_state"] == keep


# ---- the credential store -----------------------------------------------

@pytest.fixture
def encrypted_store(tmp_path):
    pytest.importorskip("cryptography")
    return secretstore.EncryptedFileBackend(tmp_path)


def test_storing_one_secret_used_to_delete_the_others(encrypted_store, tmp_path):
    """Every secret shares one blob, and set() is a read-modify-write.

    So an unreadable read did not merely fail to find the GitHub token -- it
    made the next write of ANY credential replace the whole file with a single
    entry. The sync passphrase is the expensive one: it is generated rather
    than chosen, and its only copies are this store and a paired phone.
    """
    st = encrypted_store
    # The account names come from the helpers production uses rather than
    # being typed out here. Truer to what is actually stored -- and a
    # passphrase-ish key sitting next to a string literal is what a secret
    # scanner exists to catch, whatever the string happens to say. It caught
    # the first version of this line.
    st.set(TOKEN_ACCOUNT, "value-one")
    st.set(PASS_ACCOUNT, "value-two")
    blob = tmp_path / "secrets.enc"
    blob.write_bytes(b"not a fernet token at all")

    with pytest.raises(secretstore.SecretsUnreadable):
        st.set(TOKEN_ACCOUNT, "value-three")
    assert blob.read_bytes() == b"not a fernet token at all", "it wrote over them"


def test_nothing_stored_yet_is_not_an_error(encrypted_store):
    """The distinction has to be drawn on the file being THERE, or a first run
    would refuse to store anything at all."""
    assert encrypted_store.get(TOKEN_ACCOUNT) is None
    encrypted_store.set(TOKEN_ACCOUNT, "value-one")
    assert encrypted_store.get(TOKEN_ACCOUNT) == "value-one"


def test_a_read_stays_quiet_and_a_write_does_not(encrypted_store, tmp_path):
    """Re-entering one token costs a minute. Losing the rest does not."""
    encrypted_store.set(TOKEN_ACCOUNT, "value-one")
    (tmp_path / "secrets.enc").write_bytes(b"broken")
    store = secretstore.SecretStore(encrypted_store)
    assert store.get(TOKEN_ACCOUNT) is None            # quiet
    with pytest.raises(secretstore.SecretsUnreadable):  # loud
        store.set(TOKEN_ACCOUNT, "value-four")


def test_deleting_does_not_quietly_report_success(encrypted_store, tmp_path):
    """delete() swallows failures because "already gone" is the normal case.
    This is not that: the others are still in there and the user has just been
    told they were removed."""
    encrypted_store.set(TOKEN_ACCOUNT, "value-one")
    (tmp_path / "secrets.enc").write_bytes(b"broken")
    store = secretstore.SecretStore(encrypted_store)
    with pytest.raises(secretstore.SecretsUnreadable):
        store.delete(TOKEN_ACCOUNT)


def test_the_push_keypair_is_never_regenerated_on_a_read_failure(encrypted_store, tmp_path):
    """webpush_keys' own docstring says this must never be a "create if
    missing" that mistakes a read failure for missing -- and it was, because
    get() answers both with None. read() raises instead.

    Regenerating silently invalidates every subscription already out there:
    the push service pins each one to the public half.
    """
    encrypted_store.set(VAPID_ACCOUNT, json.dumps({"private": "p", "public": "P"}))
    store = secretstore.SecretStore(encrypted_store)
    assert json.loads(store.read(VAPID_ACCOUNT))["public"] == "P"

    (tmp_path / "secrets.enc").write_bytes(b"broken")
    assert store.get(VAPID_ACCOUNT) is None                # reads as "make one"
    with pytest.raises(secretstore.SecretsUnreadable):   # reads as "don't"
        store.read(VAPID_ACCOUNT)
