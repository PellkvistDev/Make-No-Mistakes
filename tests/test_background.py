"""The DEFAULT background is served by CSS straight from disk, so a fresh
install always shows it. get_background() therefore returns "" for the default
case (meaning "use the CSS default") and a data URI only for a custom image --
and it must NEVER raise, even if the configured file is gone or unreadable.
(Regression: encoding a missing/blank default here left fresh installs with no
background at all.)"""

import sys
import types
from pathlib import Path

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402

_PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def _api(bg_path=""):
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = types.SimpleNamespace(background_path=bg_path)
    return api


def test_default_background_returns_empty_string():
    # No custom path -> "" (the CSS default is what actually renders).
    assert _api("").get_background() == ""


def test_custom_background_returns_data_uri(tmp_path):
    img = tmp_path / "wall.png"
    img.write_bytes(_PNG)
    uri = _api(str(img)).get_background()
    assert uri.startswith("data:image/png;base64,")


def test_missing_custom_file_falls_back_to_default_not_error():
    # A configured background that no longer exists must NOT raise; it falls
    # back to the CSS default ("").
    assert _api("/no/such/wallpaper.png").get_background() == ""


def test_unreadable_custom_file_never_raises(tmp_path, monkeypatch):
    img = tmp_path / "wall.png"
    img.write_bytes(_PNG)
    monkeypatch.setattr(gui_app, "_data_uri",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")))
    assert _api(str(img)).get_background() == ""


def test_the_default_image_actually_ships_next_to_the_page():
    # The CSS references url("bg-default.jpg") relative to style.css, so the
    # file must physically live in the web dir.
    assert (gui_app.WEB_DIR / "bg-default.jpg").is_file()
    css = (gui_app.WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert 'url("bg-default.jpg")' in css
