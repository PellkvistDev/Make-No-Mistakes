"""Codebase memory: identifier-aware tokenising, TF-IDF ranking that surfaces
the right file, incremental re-indexing, persistence, and the search_code tool."""

import glmcode.codebase_memory as cm
from glmcode.codebase_memory import CodebaseIndex, tokenize


def test_tokenize_splits_identifiers():
    toks = set(tokenize("getUserName user_name  ParseHTTPResponse"))
    assert {"getusername", "get", "user", "name"} <= toks
    assert {"user_name"} <= toks or {"user", "name"} <= toks   # snake split
    assert {"parsehttpresponse", "parse", "http", "response"} <= toks


def _write(root, name, text):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _index(tmp_path, monkeypatch):
    # Keep the index cache OUTSIDE the indexed folder (as in production, where it
    # lives under CONFIG_DIR) so it doesn't index itself.
    monkeypatch.setattr(cm, "MEMORY_DIR", tmp_path.with_name(tmp_path.name + "_mem"))
    return CodebaseIndex(tmp_path)


def test_search_surfaces_the_relevant_file(tmp_path, monkeypatch):
    _write(tmp_path, "auth.py", "def login(user, password):\n    return check_password(user, password)\n")
    _write(tmp_path, "math_utils.py", "def add(a, b):\n    return a + b\n")
    _write(tmp_path, "render.py", "def draw(canvas):\n    canvas.fill('blue')\n")
    idx = _index(tmp_path, monkeypatch)
    idx.refresh()
    hits = idx.search("password login authentication", k=3)
    assert hits and hits[0]["path"] == "auth.py"
    assert hits[0]["start"] >= 1 and hits[0]["end"] >= hits[0]["start"]


def test_search_empty_query_or_corpus(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    idx.refresh()
    assert idx.search("anything") == []          # no files
    _write(tmp_path, "a.py", "x = 1\n")
    idx.refresh()
    assert idx.search("") == []                  # empty query


def test_incremental_reindex_only_touches_changed(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "alpha\n")
    _write(tmp_path, "b.py", "beta\n")
    idx = _index(tmp_path, monkeypatch)
    assert idx.refresh() == 2                     # both indexed first time
    assert idx.refresh() == 0                     # nothing changed -> no re-read
    _write(tmp_path, "b.py", "beta gamma delta\n")
    assert idx.refresh() == 1                     # only b.py re-indexed


def test_deleted_file_drops_out(tmp_path, monkeypatch):
    a = _write(tmp_path, "a.py", "keyword_alpha\n")
    _write(tmp_path, "b.py", "other\n")
    idx = _index(tmp_path, monkeypatch)
    idx.refresh()
    assert idx.search("keyword_alpha")
    a.unlink()
    idx.refresh()
    assert idx.search("keyword_alpha") == []


def test_index_persists_across_instances(tmp_path, monkeypatch):
    _write(tmp_path, "a.py", "persisted_token_here\n")
    idx = _index(tmp_path, monkeypatch)
    idx.refresh()
    # A brand-new index over the same root loads the cache and needs no re-read.
    idx2 = _index(tmp_path, monkeypatch)
    assert idx2.refresh() == 0
    assert idx2.search("persisted_token_here")


def test_binary_and_ignored_files_are_skipped(tmp_path, monkeypatch):
    _write(tmp_path, "code.py", "real_source_line\n")
    (tmp_path / "node_modules").mkdir()
    _write(tmp_path, "node_modules/dep.js", "should_be_ignored\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary\x00stuff")
    idx = _index(tmp_path, monkeypatch)
    idx.refresh()
    paths = {p for p, _ in idx._corpus()}
    assert "code.py" in paths
    assert not any("node_modules" in p for p in paths)
    assert "blob.bin" not in paths


def test_search_code_tool(tmp_path, monkeypatch):
    import glmcode.tools as tools
    monkeypatch.setattr(cm, "MEMORY_DIR", tmp_path.with_name(tmp_path.name + "_mem"))
    cm._indexes.clear()
    _write(tmp_path, "widget.py", "def render_widget(theme):\n    return theme.color\n")
    tools.set_workdir(tmp_path)
    out = tools.search_code("render widget theme")
    assert "widget.py" in out and "render_widget" in out
