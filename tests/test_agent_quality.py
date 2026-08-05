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


# ------------------------------------------- a check that fell over vs passed
# '' means "checked, and it's fine", and the agent trusts that. Anything that
# stops the check from running has to look different, or a broken file sails
# through under a reassuring silence -- which is exactly how a node timeout
# once reported success.

def test_source_with_nul_bytes_is_reported_not_silently_passed(tmp_path, monkeypatch):
    """Already handled -- compile() raises SyntaxError for this on supported
    Pythons, so the existing branch catches it. Kept because it is a file the
    agent must never be told is fine, and because it is the kind of thing that
    would otherwise depend on which exception a future Python chooses; the
    catch-all now reports rather than passes either way."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "broken.py"
    f.write_bytes(b"x = 1\n\x00\ny = 2\n")
    out = tools._syntax_feedback(f)
    assert out.strip(), "a file Python cannot compile came back as fine"
    assert "WARNING" in out or "unchecked" in out


def test_a_checker_that_blows_up_says_so_instead_of_passing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n", encoding="utf-8")

    def boom(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(type(f), "read_text", boom)
    out = tools._syntax_feedback(f)
    assert "unchecked" in out, f"an unreadable file was reported as fine: {out!r}"
    assert "OSError" in out


def test_node_that_cannot_run_says_so_instead_of_passing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "mod.js"
    f.write_text("var x = 1;\n", encoding="utf-8")
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/node")

    def boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(tools.subprocess, "run", boom)
    out = tools._syntax_feedback(f)
    assert "unchecked" in out, f"an unrunnable checker was reported as fine: {out!r}"


def test_a_file_too_big_to_check_is_still_silent(tmp_path, monkeypatch):
    """Deliberately skipping is not the same as failing: no note for this."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "huge.py"
    f.write_text("x = 1\n" * 200_000, encoding="utf-8")
    assert f.stat().st_size > tools._MAX_CHECK_BYTES
    assert tools._syntax_feedback(f) == ""
