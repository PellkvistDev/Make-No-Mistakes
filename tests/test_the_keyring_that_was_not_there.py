"""`import keyring` proves the package is installed, not that it works.

Found by the setup check, on the machine running the tests, which is the kind
of place this matters: `keyring` installs happily where there is no OS
credential store behind it -- a headless Linux box, a container, any desktop
without gnome-keyring or kwallet -- and then raises NoKeyringError on every
actual call.

Selecting it anyway was silent and total:

  * Settings reported the backend as `keyring`, and secure;
  * `get` swallowed the error and answered None for every credential, so the
    GitHub token read as "not set" no matter how many times it was entered;
  * `set` raised out of the Settings button with nothing shown;
  * and the encrypted-file backend, which exists for exactly this machine, was
    never reached.

One read of a name that does not exist, once, at construction. It costs
nothing and it is the difference between working and appearing to.
"""

import sys
import types

import pytest

from glmcode import githubsync, secretstore

# The name the app really files it under, rather than a credential-shaped
# literal sitting next to a string -- which is what a secret scanner catches,
# whatever the string says.
TOKEN_ACCOUNT = githubsync._account("github.com")


class NoKeyringError(Exception):
    """What the real package raises when nothing is behind it."""


def _fake_keyring(*, works: bool):
    def get_password(service, account):
        if not works:
            raise NoKeyringError("No recommended backend was available.")
        return store.get((service, account))

    def set_password(service, account, secret):
        if not works:
            raise NoKeyringError("No recommended backend was available.")
        store[(service, account)] = secret

    def delete_password(service, account):
        if not works:
            raise NoKeyringError("No recommended backend was available.")
        store.pop((service, account), None)

    store: dict = {}
    mod = types.SimpleNamespace(get_password=get_password,
                                set_password=set_password,
                                delete_password=delete_password)
    mod._store = store
    return mod


@pytest.fixture
def with_keyring(monkeypatch):
    def install(works):
        monkeypatch.setitem(sys.modules, "keyring", _fake_keyring(works=works))
    return install


def test_a_keyring_with_nothing_behind_it_is_not_chosen(with_keyring):
    with_keyring(works=False)
    with pytest.raises(NoKeyringError):
        secretstore.KeyringBackend()


def test_a_working_one_still_is(with_keyring):
    with_keyring(works=True)
    b = secretstore.KeyringBackend()
    b.set(TOKEN_ACCOUNT, "a-value")
    assert b.get(TOKEN_ACCOUNT) == "a-value"


def test_the_probe_stores_nothing(with_keyring):
    """It is a READ of a name that does not exist. A write would leave a stray
    entry in the user's credential store on every launch."""
    with_keyring(works=True)
    secretstore.KeyringBackend()
    assert sys.modules["keyring"]._store == {}


def test_the_fallback_that_exists_for_this_machine_is_reached(with_keyring, monkeypatch, tmp_path):
    """The whole consequence. Without the probe this returned the keyring
    backend and the app could not store a single credential."""
    pytest.importorskip("cryptography")
    with_keyring(works=False)
    monkeypatch.setattr(secretstore, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(secretstore, "_store", None, raising=False)
    backend = secretstore._best_backend()
    assert backend.name == "encrypted-file"
    backend.set(TOKEN_ACCOUNT, "a-value")
    assert backend.get(TOKEN_ACCOUNT) == "a-value"


def test_and_the_app_stops_claiming_it_is_secure(with_keyring, monkeypatch, tmp_path):
    """`is_secure` drives a warning in Settings about where secrets live. It
    said the strong path was in use on a machine that had no such path."""
    pytest.importorskip("cryptography")
    with_keyring(works=False)
    monkeypatch.setattr(secretstore, "CONFIG_DIR", tmp_path)
    store = secretstore.SecretStore(secretstore._best_backend())
    assert store.is_secure is False
    assert store.backend_name == "encrypted-file"
