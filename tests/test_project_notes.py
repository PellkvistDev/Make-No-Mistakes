"""Zero-config project onboarding: offer to write a GLM.md only for a real
project folder that doesn't already have agent notes."""

import sys
import types

sys.modules.setdefault("webview", types.ModuleType("webview"))

from glmcode.gui import app  # noqa: E402
from glmcode.prompts import GLM_MD_TASK  # noqa: E402


def test_needs_notes_true_for_project_without_notes(tmp_path):
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    assert app.Api._needs_project_notes(tmp_path) is True


def test_needs_notes_false_when_glm_md_exists(tmp_path):
    (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "GLM.md").write_text("# notes\n", encoding="utf-8")
    assert app.Api._needs_project_notes(tmp_path) is False


def test_needs_notes_false_for_other_agent_md(tmp_path):
    (tmp_path / "main.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# notes\n", encoding="utf-8")
    assert app.Api._needs_project_notes(tmp_path) is False


def test_needs_notes_false_for_empty_or_dotfile_only(tmp_path):
    assert app.Api._needs_project_notes(tmp_path) is False   # empty
    (tmp_path / ".gitignore").write_text("x\n", encoding="utf-8")
    assert app.Api._needs_project_notes(tmp_path) is False   # only hidden files


def test_needs_notes_false_for_whiteboard(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "WHITEBOARD_DIR", tmp_path)
    (tmp_path / "scratch.py").write_text("x\n", encoding="utf-8")
    assert app.Api._needs_project_notes(tmp_path) is False


def test_glm_md_task_is_actionable():
    assert "GLM.md" in GLM_MD_TASK and "write_file" in GLM_MD_TASK
