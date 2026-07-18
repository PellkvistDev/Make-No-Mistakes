"""attach_paths: drag & drop file paths become the same attachment shape the
file picker produces (thumbs for images, missing files skipped)."""

import base64
import sys
import types

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def test_attach_paths_shapes_and_filters(tmp_path):
    api = gui_app.Api.__new__(gui_app.Api)
    doc = tmp_path / "notes.txt"
    doc.write_text("x", encoding="utf-8")
    img = tmp_path / "shot.png"
    img.write_bytes(_PNG)

    res = api.attach_paths([str(doc), str(img), str(tmp_path / "missing.bin")])
    assert [a["name"] for a in res] == ["notes.txt", "shot.png"]
    assert res[0]["thumb"] == ""                       # non-image: generic chip
    assert res[1]["thumb"].startswith("data:image")    # image: real thumbnail
    assert all(a["path"] for a in res)


def test_attach_paths_empty_and_garbage():
    api = gui_app.Api.__new__(gui_app.Api)
    assert api.attach_paths([]) == []
    assert api.attach_paths(None) == []
    assert api.attach_paths(["/definitely/not/real"]) == []


class _FakeWindow:
    def __init__(self):
        self.scripts = []

    def evaluate_js(self, script):
        self.scripts.append(script)


def test_on_drop_reads_pywebview_paths_and_calls_back(tmp_path):
    """The dropped files' REAL paths live only in pywebview's python-side
    event (pywebviewFullPath) -- _on_drop reads them there and hands the
    resolved attachments back to the page via __onDropResult."""
    import json
    api = gui_app.Api.__new__(gui_app.Api)
    api._window = _FakeWindow()
    doc = tmp_path / "notes.txt"
    doc.write_text("x", encoding="utf-8")

    event = {"dataTransfer": {"files": [
        {"name": "notes.txt", "pywebviewFullPath": str(doc)},
        {"name": "nofilepath"},  # a File without the injected path -> skipped
    ]}}
    api._on_drop(event)

    assert len(api._window.scripts) == 1
    script = api._window.scripts[0]
    assert script.startswith("window.__onDropResult(")
    payload = json.loads(script[script.index("(") + 1:script.rindex(")")])
    assert [a["name"] for a in payload] == ["notes.txt"]


def test_on_drop_never_raises():
    api = gui_app.Api.__new__(gui_app.Api)
    api._window = None
    api._on_drop(None)
    api._on_drop({})
    api._on_drop({"dataTransfer": {"files": [{"pywebviewFullPath": None}]}})
