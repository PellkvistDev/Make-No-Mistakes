"""Opening a project folder the app already knows about.

new_session() opens an OS folder dialog, which is right the first time and
wrong every time after: the path of every project you have worked in is already
sitting in the session list. new_session_in() is the same thing without the
question -- but it takes a path from stored state, so it has to check it rather
than believe it.
"""

import types

import pytest

from glmcode import config as cfgmod


def _api(monkeypatch, **cfg_kw):
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app
    api = gui_app.Api.__new__(gui_app.Api)
    c = cfgmod.Config()
    for k, v in cfg_kw.items():
        setattr(c, k, v)
    api._cfg = c
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    monkeypatch.setattr(gui_app.Api, "list_sessions", lambda self: [])
    activated = {}

    def fake_activate(self, sid, msgs, cwd, a, b, c_, auto_backup=True):
        activated.update(cwd=cwd, auto_backup=auto_backup)
        return {"ok": True, "cwd": cwd}

    monkeypatch.setattr(gui_app.Api, "_activate_session", fake_activate)
    return api, activated


def test_a_known_folder_opens_without_a_dialog(monkeypatch, tmp_path):
    api, activated = _api(monkeypatch)
    res = api.new_session_in(str(tmp_path))
    assert "error" not in res
    assert activated["cwd"] == str(tmp_path)


def test_the_backup_choice_is_carried_through(monkeypatch, tmp_path):
    api, activated = _api(monkeypatch)
    api.new_session_in(str(tmp_path), False)
    assert activated["auto_backup"] is False


def test_a_folder_that_has_gone_away_is_reported_not_created(monkeypatch, tmp_path):
    """Folders get moved, renamed and deleted between one week and the next.
    The agent's whole idea of a workspace is its working directory, so starting
    a chat in a path that is not there would be worse than refusing."""
    api, activated = _api(monkeypatch)
    res = api.new_session_in(str(tmp_path / "since-deleted"))
    assert "error" in res and "not found" in res["error"]
    assert not activated, "a session was started in a folder that does not exist"


def test_a_file_is_not_a_project_folder(monkeypatch, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    api, activated = _api(monkeypatch)
    assert "error" in api.new_session_in(str(f))
    assert not activated


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_empty_path_is_refused_rather_than_treated_as_the_cwd(monkeypatch, bad):
    """Path("") is Path("."), so a missing value would silently open a chat in
    whatever directory the app happens to be running from."""
    api, activated = _api(monkeypatch)
    assert "error" in api.new_session_in(bad)
    assert not activated
