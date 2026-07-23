"""Secure storage for small secrets (currently: GitHub tokens).

A token must never sit in config.json (which is world-readable plaintext and
easy to accidentally share, back up, or commit). This module keeps secrets out
of it entirely, preferring the OS credential store and degrading gracefully:

  1. OS keyring  -- Windows Credential Manager / macOS Keychain / Secret
     Service. DPAPI/Keychain-backed, tied to the OS user. The right answer.
  2. Encrypted file -- if no keyring is installed: a Fernet-encrypted blob in
     CONFIG_DIR with 0600 perms. Weaker (the key lives on the same disk), but
     far better than plaintext -- it survives casual inspection, log leaks and
     accidental commits. The UI warns when this backend is in use.
  3. Memory -- tests only; never touches disk.

Callers get a SecretStore via get_store(); backend_name lets the UI tell the
user how their token is protected.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from .config import CONFIG_DIR

# One service namespace in the OS keyring; accounts are per-secret keys.
_SERVICE = "make-no-mistakes"


def _lock_down(path: Path) -> None:
    """Best-effort 0600 so other local users can't read a secret file. No-op
    where chmod is meaningless (Windows ignores POSIX bits, but the file still
    lands under the user's profile)."""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


class _Backend:
    name = "none"

    def get(self, account: str) -> str | None: ...
    def set(self, account: str, secret: str) -> None: ...
    def delete(self, account: str) -> None: ...


class MemoryBackend(_Backend):
    """In-process only; for tests and as a last-ditch fallback."""
    name = "memory"

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, account: str) -> str | None:
        return self._d.get(account)

    def set(self, account: str, secret: str) -> None:
        self._d[account] = secret

    def delete(self, account: str) -> None:
        self._d.pop(account, None)


class KeyringBackend(_Backend):
    """OS credential store via the `keyring` package."""
    name = "keyring"

    def __init__(self) -> None:
        import keyring  # noqa: F401  (import here so absence -> unavailable)
        self._keyring = keyring

    def get(self, account: str) -> str | None:
        return self._keyring.get_password(_SERVICE, account)

    def set(self, account: str, secret: str) -> None:
        self._keyring.set_password(_SERVICE, account, secret)

    def delete(self, account: str) -> None:
        try:
            self._keyring.delete_password(_SERVICE, account)
        except Exception:
            pass  # already gone


class EncryptedFileBackend(_Backend):
    """Fernet-encrypted JSON blob in CONFIG_DIR, key stored beside it (both
    0600). The key-on-disk means this protects against casual reads, log/commit
    leaks and shoulder-surfing -- NOT against another process running as the
    same OS user. The UI surfaces that trade-off (name == 'encrypted-file')."""
    name = "encrypted-file"

    def __init__(self, directory: Path | None = None) -> None:
        from cryptography.fernet import Fernet  # raises if unavailable
        self._Fernet = Fernet
        d = directory or CONFIG_DIR
        d.mkdir(parents=True, exist_ok=True)
        self._key_path = d / ".secret.key"
        self._blob_path = d / "secrets.enc"
        self._fernet = self._Fernet(self._load_or_make_key())

    def _load_or_make_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        key = self._Fernet.generate_key()
        self._key_path.write_bytes(key)
        _lock_down(self._key_path)
        return key

    def _read(self) -> dict:
        if not self._blob_path.exists():
            return {}
        try:
            raw = self._fernet.decrypt(self._blob_path.read_bytes())
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            # Corrupt or key-mismatch: treat as empty rather than crash. The
            # user just re-enters the token; nothing else depends on it.
            return {}

    def _write(self, data: dict) -> None:
        token = self._fernet.encrypt(json.dumps(data).encode("utf-8"))
        tmp = self._blob_path.parent / (self._blob_path.name + ".tmp")
        tmp.write_bytes(token)
        _lock_down(tmp)
        os.replace(tmp, self._blob_path)
        _lock_down(self._blob_path)

    def get(self, account: str) -> str | None:
        return self._read().get(account)

    def set(self, account: str, secret: str) -> None:
        data = self._read()
        data[account] = secret
        self._write(data)

    def delete(self, account: str) -> None:
        data = self._read()
        if data.pop(account, None) is not None:
            self._write(data)


class SecretStore:
    """Thin front over whichever backend is active."""

    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return self._backend.name

    @property
    def is_secure(self) -> bool:
        """True when secrets live in the OS credential store (the strong path).
        False for the encrypted-file / memory fallbacks -- the UI warns then."""
        return self._backend.name == "keyring"

    def get(self, account: str) -> str | None:
        try:
            return self._backend.get(account)
        except Exception:
            return None

    def set(self, account: str, secret: str) -> None:
        self._backend.set(account, secret)

    def delete(self, account: str) -> None:
        try:
            self._backend.delete(account)
        except Exception:
            pass


def _best_backend() -> _Backend:
    try:
        return KeyringBackend()
    except Exception:
        pass
    # A broken native cryptography build can raise a Rust PanicException, which
    # is a BaseException (not Exception) -- catch it too so we degrade to memory
    # instead of taking the whole app down. KeyboardInterrupt/SystemExit still
    # propagate.
    try:
        return EncryptedFileBackend()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        pass
    return MemoryBackend()


_store: SecretStore | None = None


def get_store() -> SecretStore:
    """Process-wide singleton, choosing the strongest available backend."""
    global _store
    if _store is None:
        _store = SecretStore(_best_backend())
    return _store


def set_store(store: SecretStore) -> None:
    """Test seam: install a specific store (e.g. a MemoryBackend one)."""
    global _store
    _store = store


def encode_account(*parts: str) -> str:
    """Stable keyring account key from arbitrary parts (host/owner/repo etc.),
    URL-safe so no backend chokes on odd characters."""
    raw = "\x1f".join(p.strip() for p in parts).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")
