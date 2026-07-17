"""@-mention files: the project-file fuzzy index, the false-positive-safe
expansion of @paths into attached file contents, and hiding that attached
context from the on-screen message."""

import pytest

from glmcode.tools import (build_file_context, project_files,
                           search_project_files)
from glmcode.prompts import FILE_CONTEXT_MARKER
from glmcode.sessions import to_display


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src" / "utils.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret", encoding="utf-8")
    return tmp_path


def test_index_excludes_ignored_and_hidden(project):
    files = set(project_files(project, force=True))
    assert files == {"src/main.py", "src/utils.py", "README.md"}
    assert not any("node_modules" in f or ".git" in f for f in files)


def test_fuzzy_search_ranks_basename_first(project):
    assert search_project_files(project, "main") == ["src/main.py"]
    assert search_project_files(project, "utils")[0] == "src/utils.py"
    # subsequence match still finds it
    assert "src/main.py" in search_project_files(project, "smain")
    # empty query returns files (shallow first)
    assert search_project_files(project, "") != []


def test_build_context_attaches_mentioned_files(project):
    ctx = build_file_context(project, "compare @src/main.py with @src/utils.py please")
    assert ctx.startswith(FILE_CONTEXT_MARKER)
    assert "### src/main.py" in ctx and "print('hi')" in ctx
    assert "### src/utils.py" in ctx and "return 1" in ctx
    assert "```python" in ctx


def test_build_context_ignores_nonexistent_and_decorators(project):
    # @property / @staticmethod / an email must NOT expand (no such file)
    assert build_file_context(project, "use @property and email a@b.com") == ""
    assert build_file_context(project, "see @src/nope.py") == ""


def test_build_context_respects_total_cap(project):
    big = "x = 1\n" * 5000
    (project / "big.py").write_text(big, encoding="utf-8")
    ctx = build_file_context(project, "@big.py", per_file=2000, total=2500)
    assert "[truncated]" in ctx  # per-file cap applied


def test_attached_context_is_hidden_from_display():
    text = "look at this" + FILE_CONTEXT_MARKER + "\n### a.py\n```\ncode\n```\n</referenced-files>"
    items = to_display([{"role": "user", "content": text}])
    user = [it for it in items if it["kind"] == "user"][0]
    assert user["text"] == "look at this"       # only the user's own words
    assert "code" not in user["text"]


def test_resolve_mentions_flags_images_and_strips_at(project):
    from glmcode.tools import resolve_mentions
    img = project / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)  # minimal PNG-ish
    text = "compare @src/main.py with @pic.png"
    ms = resolve_mentions(project, text)
    by_rel = {m["rel"]: m for m in ms}
    assert by_rel["src/main.py"]["is_image"] is False
    assert by_rel["pic.png"]["is_image"] is True
    # the caller strips '@' using the token -> a clean path the model can use
    for m in ms:
        text = text.replace("@" + m["token"], m["rel"])
    assert "@" not in text
    assert "src/main.py" in text and "pic.png" in text


def test_text_context_excludes_images(project):
    from glmcode.tools import build_text_file_context, resolve_mentions
    img = project / "pic.png"
    img.write_bytes(b"\x00\x01\x02")
    ms = resolve_mentions(project, "@src/main.py @pic.png")
    block = build_text_file_context([(m["rel"], m["path"]) for m in ms
                                     if not m["is_image"]])
    assert "src/main.py" in block and "print('hi')" in block
    assert "pic.png" not in block  # image never inlined as text
