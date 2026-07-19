"""The saved agent-browser profile ('Remember browser logins'): logins live in
a dedicated directory, and clear_browser_profile wipes it -- the escape hatch
that keeps the feature from being a one-way door."""

import sys
import types
from types import SimpleNamespace

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402


def _api(monkeypatch, tmp_path, chats=None):
    monkeypatch.setattr(gui_app, "CONFIG_DIR", tmp_path)
    api = gui_app.Api.__new__(gui_app.Api)
    api._chats = chats or {}
    return api


def test_clear_deletes_the_profile_dir(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    prof = tmp_path / "browser-profile"
    (prof / "Default").mkdir(parents=True)
    (prof / "Default" / "Cookies").write_bytes(b"secret")
    res = api.clear_browser_profile()
    assert res == {"ok": True}
    assert not prof.exists()


def test_clear_is_fine_when_nothing_saved(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    assert api.clear_browser_profile() == {"ok": True}


def test_clear_refuses_while_a_browser_is_open(monkeypatch, tmp_path):
    open_sess = SimpleNamespace(is_open=True)
    chats = {"s1": SimpleNamespace(agent=SimpleNamespace(browser_session=open_sess))}
    api = _api(monkeypatch, tmp_path, chats)
    prof = tmp_path / "browser-profile"
    prof.mkdir()
    res = api.clear_browser_profile()
    assert "error" in res and "close" in res["error"]
    assert prof.exists()   # nothing deleted under a live browser

    # once the browser is closed, clearing works
    open_sess.is_open = False
    assert api.clear_browser_profile() == {"ok": True}
    assert not prof.exists()
