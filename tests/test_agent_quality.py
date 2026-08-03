"""Agent-quality scaffolding: post-write syntax verification and the
project-layout map in the system prompt."""

import shutil

import pytest

import glmcode.tools as tools
from glmcode.prompts import _project_map, build_system_prompt


# ---------------------------------------------------------------- syntax --

def test_broken_python_edit_warns_in_tool_result(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "mod.py"
    f.write_text("def ok():\n    return 1\n", encoding="utf-8")
    out = tools.edit_file(str(f), "return 1", "return (1")
    assert "WARNING" in out and "Python syntax error" in out
    assert "line" in out
    # the write itself still happened (report, don't block)
    assert "return (1" in f.read_text(encoding="utf-8")


def test_valid_python_write_has_no_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tools.write_file(str(tmp_path / "good.py"), "x = 1\n")
    assert "WARNING" not in out


def test_broken_json_write_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tools.write_file(str(tmp_path / "cfg.json"), '{"a": 1,}')
    assert "WARNING" in out and "JSON" in out


def test_broken_toml_write_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tools.write_file(str(tmp_path / "cfg.toml"), "a = [1,\n")
    assert "WARNING" in out and "TOML" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_broken_js_write_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tools.write_file(str(tmp_path / "app.js"), "function f( {\n")
    assert "WARNING" in out and "JavaScript" in out


def test_unknown_extension_never_warns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tools.write_file(str(tmp_path / "notes.txt"), "{{{ not code )))")
    assert "WARNING" not in out


# ------------------------------------------------------------------- map --

def make_tree(root):
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("x", encoding="utf-8")
    (root / "src" / "util.py").write_text("x", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "guide.md").write_text("x", encoding="utf-8")
    (root / "README.md").write_text("x", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("x", encoding="utf-8")


def test_map_lists_tree_and_skips_ignored(tmp_path):
    make_tree(tmp_path)
    m = _project_map(tmp_path)
    assert "# Project layout" in m
    assert "src/" in m and "main.py" in m and "README.md" in m
    assert "node_modules" not in m
    assert ".git" not in m


def test_map_caps_entries(tmp_path):
    for i in range(100):
        (tmp_path / f"file{i:03}.txt").write_text("x", encoding="utf-8")
    m = _project_map(tmp_path, per_dir=15, max_entries=60)
    listed = [l for l in m.splitlines() if l.startswith("file")]
    assert len(listed) <= 15
    assert "more entries not shown" in m


def test_map_empty_dir_is_omitted(tmp_path):
    assert _project_map(tmp_path) == ""


def test_map_included_in_system_prompt(tmp_path):
    make_tree(tmp_path)
    sp = build_system_prompt(tmp_path, "test-model")
    assert "# Project layout" in sp and "main.py" in sp


def test_js_check_timeout_says_so_instead_of_looking_clean(tmp_path, monkeypatch):
    """A timed-out check must not return the same '' that means "looks fine" --
    the write would read as verified when nothing actually verified it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools.shutil, "which", lambda n: "/usr/bin/node")

    def slow(*a, **kw):
        raise tools.subprocess.TimeoutExpired(cmd="node", timeout=30)
    monkeypatch.setattr(tools.subprocess, "run", slow)

    out = tools.write_file(str(tmp_path / "app.js"), "function f() {}\n")
    assert "couldn't syntax-check" in out
