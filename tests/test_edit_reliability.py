"""edit_file's whitespace-tolerant fallback: an old_string whose only fault
is surrounding whitespace/indentation should still land (uniquely), instead
of hard-failing and costing the model a full re-read + retry."""

import pytest

import glmcode.tools as tools
from glmcode.tools import _flexible_line_match, edit_file


def test_exact_match_still_preferred(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n", encoding="utf-8")
    out = edit_file(str(f), "x = 1", "x = 42")
    assert "matched\n            ignoring" not in out  # not the flexible path
    assert f.read_text(encoding="utf-8") == "x = 42\ny = 2\n"


def test_trailing_whitespace_mismatch_still_edits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    # file has NO trailing spaces; the model's old_string adds them
    f.write_text("def f():\n    return 1\n", encoding="utf-8")
    out = edit_file(str(f), "def f():   \n    return 1   ", "def f():\n    return 2")
    assert "ignoring surrounding whitespace" in out
    assert f.read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_wrong_indentation_matches_and_uses_new_text(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("class C:\n    def m(self):\n        return 1\n", encoding="utf-8")
    # model under-indents its old_string (common), but the lines are unique
    out = edit_file(str(f), "def m(self):\n    return 1",
                    "def m(self):\n        return 99")
    assert "ignoring surrounding whitespace" in out
    assert "return 99" in f.read_text(encoding="utf-8")


def test_leading_and_trailing_blank_padding_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    # blank-line padding AND a trailing space -> not an exact match, so the
    # flexible path (which trims blank edges) is what lands it.
    out = edit_file(str(f), "  \nb = 2  \n  ", "b = 20")
    assert "ignoring surrounding whitespace" in out
    assert f.read_text(encoding="utf-8") == "a = 1\nb = 20\nc = 3\n"


def test_ambiguous_flexible_match_refuses_without_replace_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("v = 1\nv = 1\n", encoding="utf-8")
    with pytest.raises(tools.ToolErrorBase) as ei:
        edit_file(str(f), "v = 1 ", "v = 2")  # trailing space -> flexible only
    assert "2 whitespace-insensitive matches" in str(ei.value)
    assert f.read_text(encoding="utf-8") == "v = 1\nv = 1\n"  # untouched


def test_ambiguous_flexible_match_ok_with_replace_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("v = 1\nv = 1\n", encoding="utf-8")
    out = edit_file(str(f), "v = 1 ", "v = 2", replace_all=True)
    assert "2 replacements" in out
    assert f.read_text(encoding="utf-8") == "v = 2\nv = 2\n"


def test_genuinely_absent_still_errors_helpfully(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(tools.ToolErrorBase) as ei:
        edit_file(str(f), "nothing like this", "y")
    assert "not found" in str(ei.value)


def test_flexible_matcher_unit():
    text = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    assert _flexible_line_match(text, "def f():\n  return 1") == [(0, 2)]
    assert _flexible_line_match(text, "return 1") == [(1, 2)]
    assert _flexible_line_match(text, "not here") == []
    # blank-only pattern collapses to nothing (never matches the whole file)
    assert _flexible_line_match(text, "\n\n") == []
