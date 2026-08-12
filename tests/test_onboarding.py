"""Onboarding must ALWAYS complete once a key is entered -- even on a
locked-down machine where `setx` hangs/is blocked, or where reading prior
sessions fails. The key goes live via os.environ regardless, so the app opens
ready to use. (Regression: an uncaught subprocess.TimeoutExpired from setx
used to reject the JS bridge, and the onboarding button silently did nothing.)"""

import os
import subprocess
import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402


def _api():
    api = gui_app.Api.__new__(gui_app.Api)
    api._client = "sentinel"
    api._cfg = types.SimpleNamespace(last_session_id="")
    return api


def test_persist_env_var_sets_process_env_even_when_setx_hangs(monkeypatch):
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="setx", timeout=8)
    monkeypatch.setattr(gui_app.subprocess, "run", hang)
    monkeypatch.setattr(gui_app.sys, "platform", "win32")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    ok = gui_app.persist_env_var("ZAI_API_KEY", "sk-test-123")
    assert ok is False                                  # persistence failed...
    assert os.environ["ZAI_API_KEY"] == "sk-test-123"   # ...but key is live now


def test_persist_env_var_survives_setx_missing(monkeypatch):
    monkeypatch.setattr(gui_app.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(gui_app.sys, "platform", "win32")
    # Through monkeypatch so it is put back. What is under test here is a
    # function whose whole job is writing to os.environ, and without this the
    # write outlives the test: ZAI_API_KEY stays "k" for the rest of the run,
    # and any later test that resolves a z.ai key gets that instead of its own.
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    assert gui_app.persist_env_var("ZAI_API_KEY", "k") is False
    assert os.environ["ZAI_API_KEY"] == "k"


def test_save_api_key_completes_when_persistence_hangs(monkeypatch):
    api = _api()
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda *a: (_ for _ in ()).throw(subprocess.TimeoutExpired("setx", 8)))
    monkeypatch.setattr(api, "_resume_last", lambda: None)
    monkeypatch.setattr(api, "list_sessions", lambda: [])

    res = api.save_api_key("sk-abc")
    assert res["ok"] is True            # onboarding still completes
    assert res["persisted"] is False
    assert res["session"] is None
    assert api._client is None          # client reset so it picks up the new key


def test_save_api_key_completes_when_resume_throws(monkeypatch):
    api = _api()
    monkeypatch.setattr(gui_app, "persist_env_var", lambda *a: True)
    monkeypatch.setattr(api, "_resume_last",
                        lambda: (_ for _ in ()).throw(OSError("session store unreadable")))
    monkeypatch.setattr(api, "list_sessions", lambda: [])

    res = api.save_api_key("sk-abc")
    assert res["ok"] is True and res["persisted"] is True
    assert res["session"] is None and res["sessions"] == []


def test_save_api_key_rejects_empty():
    assert gui_app.Api.__new__(gui_app.Api).save_api_key("   ") == {"error": "empty key"}


def test_save_api_key_happy_path(monkeypatch):
    api = _api()
    monkeypatch.setattr(gui_app, "persist_env_var", lambda *a: True)
    monkeypatch.setattr(api, "_resume_last", lambda: {"id": "s1"})
    monkeypatch.setattr(api, "list_sessions", lambda: [{"id": "s1"}])
    res = api.save_api_key("  sk-xyz  ")
    assert res == {"ok": True, "persisted": True,
                   "session": {"id": "s1"}, "sessions": [{"id": "s1"}]}
