"""replace_in_files: bulk find-and-replace across a tree — literal + regex,
glob scoping, dry-run preview, and binary/ignored skipping."""

import glmcode.tools as tools


def _write(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_literal_replace_across_files(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "a.py", "call oldName()\noldName\n")
    _write(tmp_path, "b.py", "x = oldName + 1\n")
    _write(tmp_path, "c.txt", "no match here\n")
    out = tools.replace_in_files("oldName", "newName")
    assert "Replaced 3 occurrence" in out and "2 file" in out
    assert (tmp_path / "a.py").read_text() == "call newName()\nnewName\n"
    assert "oldName" not in (tmp_path / "b.py").read_text()


def test_dry_run_does_not_write(tmp_path):
    tools.set_workdir(tmp_path)
    f = _write(tmp_path, "a.py", "foo foo foo\n")
    out = tools.replace_in_files("foo", "bar", dry_run=True)
    assert "Would replace 3" in out and "dry run" in out
    assert f.read_text() == "foo foo foo\n"        # unchanged


def test_glob_scopes_by_filename(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "keep.py", "token\n")
    _write(tmp_path, "skip.js", "token\n")
    tools.replace_in_files("token", "X", glob="*.py")
    assert (tmp_path / "keep.py").read_text() == "X\n"
    assert (tmp_path / "skip.js").read_text() == "token\n"


def test_regex_with_groups(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "a.py", "color: red;\ncolor: blue;\n")
    out = tools.replace_in_files(r"color: (\w+);", r"colour: \1;", regex=True)
    assert "Replaced 2" in out
    assert (tmp_path / "a.py").read_text() == "colour: red;\ncolour: blue;\n"


def test_invalid_regex_errors(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "a.py", "x\n")
    import pytest
    with pytest.raises(tools.ToolErrorBase):
        tools.replace_in_files("(unclosed", "y", regex=True)


def test_skips_ignored_and_binary(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "src.py", "needle\n")
    _write(tmp_path, "node_modules/dep.js", "needle\n")   # ignored dir
    (tmp_path / "blob.bin").write_bytes(b"needle\x00\x01binary")
    out = tools.replace_in_files("needle", "pin")
    assert "1 file" in out
    assert (tmp_path / "node_modules/dep.js").read_text() == "needle\n"
    assert b"needle" in (tmp_path / "blob.bin").read_bytes()   # binary untouched


def test_no_matches_reports_cleanly(tmp_path):
    tools.set_workdir(tmp_path)
    _write(tmp_path, "a.py", "hello\n")
    out = tools.replace_in_files("absent", "x")
    assert "No occurrences" in out
