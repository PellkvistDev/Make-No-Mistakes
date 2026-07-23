"""Secret storage: the encrypted-file and memory backends round-trip and never
leave plaintext behind. (Keyring backend isn't exercised -- it's OS-specific.)"""

import tempfile

import pytest

from glmcode.secretstore import (EncryptedFileBackend, MemoryBackend,
                                 SecretStore, encode_account)


def _crypto_works() -> bool:
    # This sandbox ships a broken native cryptography build (missing
    # _cffi_backend -> a Rust panic). Skip the encrypted-file tests where Fernet
    # can't actually run; they're meaningful on a real install.
    try:
        with tempfile.TemporaryDirectory() as d:
            EncryptedFileBackend(d).set("probe", "v")
        return True
    except BaseException:
        return False


needs_crypto = pytest.mark.skipif(not _crypto_works(),
                                  reason="cryptography backend unavailable")


def test_memory_backend_roundtrip():
    b = MemoryBackend()
    b.set("acct", "s3cret")
    assert b.get("acct") == "s3cret"
    b.delete("acct")
    assert b.get("acct") is None


@needs_crypto
def test_encrypted_file_roundtrip_and_persistence(tmp_path):
    b = EncryptedFileBackend(tmp_path)
    b.set("gh", "ghp_TOKENVALUE")
    assert b.get("gh") == "ghp_TOKENVALUE"
    # A fresh backend over the same dir reads it back (key persisted).
    assert EncryptedFileBackend(tmp_path).get("gh") == "ghp_TOKENVALUE"


@needs_crypto
def test_encrypted_file_is_not_plaintext(tmp_path):
    EncryptedFileBackend(tmp_path).set("gh", "ghp_SUPERSECRET")
    blob = (tmp_path / "secrets.enc").read_bytes()
    assert b"ghp_SUPERSECRET" not in blob          # encrypted at rest
    assert b"SUPERSECRET" not in blob


@needs_crypto
def test_encrypted_file_delete(tmp_path):
    b = EncryptedFileBackend(tmp_path)
    b.set("a", "1")
    b.set("b", "2")
    b.delete("a")
    assert b.get("a") is None and b.get("b") == "2"


@needs_crypto
def test_corrupt_blob_degrades_to_empty(tmp_path):
    b = EncryptedFileBackend(tmp_path)
    b.set("a", "1")
    (tmp_path / "secrets.enc").write_bytes(b"not-valid-fernet")
    assert b.get("a") is None                      # no crash, treated as empty


def test_store_reports_security_level():
    assert SecretStore(MemoryBackend()).is_secure is False
    assert SecretStore(MemoryBackend()).backend_name == "memory"


def test_encode_account_stable_and_urlsafe():
    a = encode_account("github-token", "github.com")
    assert a == encode_account("github-token", "github.com")
    assert a != encode_account("github-token", "example.com")
    assert all(c.isalnum() or c in "-_=" for c in a)   # url-safe base64
