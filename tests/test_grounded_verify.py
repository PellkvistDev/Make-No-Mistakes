"""Grounded auto-verify: the verify nudge names the project's real check
command (pytest / npm test / cargo test / go test) when one is detectable."""

import json

from glmcode.prompts import VERIFY_NUDGE, detect_check_command, verify_nudge


def test_detects_pytest_from_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    assert detect_check_command(tmp_path) == "pytest -q"


def test_detects_pytest_from_conftest(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    assert detect_check_command(tmp_path) == "pytest -q"


def test_detects_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
    assert detect_check_command(tmp_path) == "pytest -q"


def test_detects_npm_test(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest", "build": "vite build"}}),
        encoding="utf-8")
    assert detect_check_command(tmp_path) == "npm test"


def test_falls_back_to_npm_build_without_test_script(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8")
    assert detect_check_command(tmp_path) == "npm run build"


def test_detects_cargo_and_go(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert detect_check_command(tmp_path) == "cargo test"
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    assert detect_check_command(tmp_path) == "go test ./..."


def test_unknown_project_has_no_command(tmp_path):
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")
    assert detect_check_command(tmp_path) == ""


def test_malformed_package_json_does_not_crash(tmp_path):
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    assert detect_check_command(tmp_path) == ""  # no test dir, bad json -> nothing


def test_verify_nudge_is_generic_when_undetectable(tmp_path):
    # Empty project: the grounded nudge is exactly the plain one, so the
    # history filter (which recognises VERIFY_NUDGE) still matches it.
    assert verify_nudge(tmp_path) == VERIFY_NUDGE


def test_verify_nudge_names_the_command_and_stays_recognisable(tmp_path):
    (tmp_path / "tests").mkdir()
    n = verify_nudge(tmp_path)
    assert "pytest -q" in n
    assert n.startswith(VERIFY_NUDGE)   # replay still treats it as internal plumbing
