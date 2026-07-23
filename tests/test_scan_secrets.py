"""scan_secrets: catches hardcoded API keys / tokens / private keys, redacts the
value, separates example/test files, and skips binaries."""

import glmcode.tools as tools


def _write(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_finds_common_secret_formats(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "config.py", "\n".join([
        "AWS = 'AKIAIOSFODNN7EXAMPLE'",
        "gh = 'ghp_" + "a" * 40 + "'",
        "api_key = 'super-secret-value-123'",
    ]))
    _write(tmp_path, "key.pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n")
    out = tools.scan_secrets()
    assert "AWS access key id" in out
    assert "GitHub token" in out
    assert "Private key block" in out
    assert "config.py:1" in out


def test_redacts_the_value(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "c.py", "token = 'ghp_" + "b" * 40 + "'")
    out = tools.scan_secrets()
    assert "bbbb" not in out                 # the raw token isn't echoed
    assert "…" in out


def test_example_files_are_separated(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "real.py", "key = 'AKIAIOSFODNN7EXAMPLE'")
    _write(tmp_path, "config.example.py", "key = 'AKIAIOSFODNN7EXAMPLE'")
    out = tools.scan_secrets()
    assert "do NOT commit" in out
    assert "example/test/doc" in out         # the .example file bucketed as soft


def test_clean_project_reports_none(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "a.py", "x = 1\nname = 'hello world'\n")
    assert "No hardcoded secrets" in tools.scan_secrets()


def test_skips_binary_and_ignored(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "src.py", "x = 1\n")
    _write(tmp_path, "node_modules/dep.js", "k = 'AKIAIOSFODNN7EXAMPLE'")
    (tmp_path / "blob.bin").write_bytes(b"AKIAIOSFODNN7EXAMPLE\x00\x01")
    out = tools.scan_secrets()
    assert "No hardcoded secrets" in out      # both the ignored dir and binary skipped
