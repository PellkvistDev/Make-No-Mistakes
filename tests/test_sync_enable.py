"""Turning shared chats on without inventing a passphrase.

It was never a password. It is the key the chats are encrypted under, and its
only job is to be identical on both devices -- pairing already carries it to
the phone. Making a person think one up bought nothing except a chance to pick
something weak, or to mistype it on the second device and fork the history into
two halves that can never read each other.

The one case that still needs a human is a store another machine already made:
that key is decided and cannot be guessed. Generating a fresh one there would
surface as `Wrong sync passphrase` -- true, and useless, for a passphrase the
user never chose -- so it is reported as what it is instead.
"""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode import config as config_mod  # noqa: E402
from glmcode import syncstore  # noqa: E402
from glmcode.gui import app as gui_app  # noqa: E402

# Worded rather than shaped like a real code: a passphrase-ish keyword
# assigned a random-looking grouped string is exactly what a secret scanner
# exists to catch, and nothing here depends on the shape.
OTHER_DEVICE_KEY = "example-key-from-the-other-device"


def _api(**attrs):
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = config_mod.Config()
    api._gh_token = lambda: "T"
    for k, v in attrs.items():
        setattr(api, k, v)
    return api


@pytest.fixture
def ok(monkeypatch):
    """Crypto healthy, a token present, nothing stored yet."""
    monkeypatch.setattr(gui_app.syncstore, "crypto_status", lambda: ("ok", ""))
    saved = []
    monkeypatch.setattr(gui_app.syncstore, "save_passphrase", lambda p: saved.append(p))
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase",
                        lambda: saved[-1] if saved else None)
    monkeypatch.setattr(gui_app.syncstore, "open_central",
                        lambda passphrase=None, token=None, api=None: (b"k", object(), True))
    return saved


# ------------------------------------------------------ the generated key

def test_turning_it_on_takes_no_passphrase_at_all(ok, monkeypatch):
    """The whole point: sync_enable has nothing to pass in."""
    monkeypatch.setattr(gui_app.syncstore, "central_has_store", lambda token=None: False)
    res = _api().sync_enable()
    assert "error" not in res, res
    assert res["passphrase_set"] is True
    assert len(ok) == 1 and ok[0]


def test_the_key_it_makes_is_not_something_a_person_would_pick():
    code = syncstore.make_passphrase()
    assert len(code.split("-")) == syncstore.RECOVERY_GROUPS
    assert all(len(g) == syncstore.RECOVERY_GROUP_LEN for g in code.split("-"))
    # The alphabet the pairing code already uses: no characters that get
    # misread, because the one time a human touches this is copying it.
    assert not (set(code) & set("IO01"))
    assert set(code.replace("-", "")) <= set(syncstore.RECOVERY_ALPHABET)


def test_it_carries_far_more_entropy_than_a_chosen_passphrase():
    import math
    bits = (syncstore.RECOVERY_GROUPS * syncstore.RECOVERY_GROUP_LEN
            * math.log2(len(syncstore.RECOVERY_ALPHABET)))
    assert bits >= 90


def test_two_machines_never_generate_the_same_key():
    """secrets, not random -- a predictable key would undo the encryption."""
    assert len({syncstore.make_passphrase() for _ in range(500)}) == 500


def test_the_generated_key_is_long_enough_for_the_store_to_accept_it():
    """open_sync refuses anything under 6 characters, so a shorter one would
    fail at the point of use rather than here."""
    assert len(syncstore.make_passphrase()) >= 6


# -------------------------------------------------- the second machine

def test_a_store_another_machine_made_asks_for_the_code(ok, monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "central_has_store", lambda token=None: True)
    res = _api().sync_enable()
    assert res["needs_code"] is True
    assert "recovery code" in res["error"]
    assert ok == [], "nothing may be stored when the key is not ours to choose"


def test_it_does_not_generate_over_an_existing_store(ok, monkeypatch):
    """The failure this prevents: a fresh key against an existing store cannot
    read a single chat in it, and reports itself as a wrong passphrase for one
    the user never typed."""
    opened = []
    monkeypatch.setattr(gui_app.syncstore, "central_has_store", lambda token=None: True)
    monkeypatch.setattr(gui_app.syncstore, "open_central",
                        lambda passphrase=None, token=None, api=None: opened.append(passphrase))
    _api().sync_enable()
    assert opened == []


def test_pasting_the_code_from_the_other_machine_works(ok, monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "central_has_store", lambda token=None: True)
    res = _api().sync_set_passphrase(OTHER_DEVICE_KEY)
    assert "error" not in res, res
    assert ok == [OTHER_DEVICE_KEY]


# ------------------------------------------------------- the recovery code

def test_the_code_can_be_read_back(ok, monkeypatch):
    """Generated means nobody has it memorised, so the only copies are this
    machine's credential store and any paired phone. It has to be visible."""
    monkeypatch.setattr(gui_app.syncstore, "central_has_store", lambda token=None: False)
    api = _api()
    api.sync_enable()
    assert api.sync_recovery_code()["code"] == ok[0]


def test_there_is_no_code_to_show_before_it_is_on(monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "load_passphrase", lambda: None)
    assert "error" in _api().sync_recovery_code()


# ------------------------------------------------- the failures still land

def test_it_still_refuses_without_a_github_token(monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "crypto_status", lambda: ("ok", ""))
    api = _api()
    api._gh_token = lambda: ""
    assert "GitHub token" in api.sync_enable()["error"]


def test_it_still_reports_the_real_crypto_reason(monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "crypto_status",
                        lambda: ("broken", "the native backend won't load"))
    assert "native backend" in _api().sync_enable()["error"]


def test_a_network_failure_is_reported_rather_than_raised(monkeypatch):
    monkeypatch.setattr(gui_app.syncstore, "crypto_status", lambda: ("ok", ""))

    def boom(token=None):
        raise syncstore.SyncError("GitHub is unreachable.")

    monkeypatch.setattr(gui_app.syncstore, "central_has_store", boom)
    assert _api().sync_enable()["error"] == "GitHub is unreachable."
