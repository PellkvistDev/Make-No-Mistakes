"""Deleting everything and reinstalling has to actually reset the app.

That is the first thing anyone does when something is wrong, and it did not
work. Two independent faults, either of which alone is enough to ruin a fresh
install, and which together produced a window that came up dead with the only
evidence in a terminal:

1. The key is persisted with `setx`, i.e. into HKCU\\Environment in the
   registry. It is not in ~/.makenomistakes and not in the app's folder, so
   deleting both leaves it behind. First-run detection asked "can I find a key
   anywhere", found that one, and skipped setup.

2. Having skipped setup, boot() went on to resume the last chat. On a fresh
   install there is none, and that branch assigned `self._agent = None` -- a
   read-only property since the multi-chat refactor. The AttributeError came
   out of boot() itself, so the page never received its settings, its session
   list, or the setup screen it should have been shown in the first place.
"""

import os
import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode import config as config_mod  # noqa: E402
from glmcode.gui import app as gui_app  # noqa: E402


def _api(cfg=None, **attrs):
    """An Api with no __init__ -- boot() only touches what is set here."""
    api = gui_app.Api.__new__(gui_app.Api)
    api._client = None
    api._cfg = cfg if cfg is not None else config_mod.Config()
    api._chats = {}
    api.session_id = None
    api._store = types.SimpleNamespace(load=lambda sid: None, list=lambda: [])
    for k, v in attrs.items():
        setattr(api, k, v)
    return api


def _boot(api, monkeypatch):
    """boot() with the parts that touch the disk and the OS stubbed out."""
    monkeypatch.setattr(api, "get_background", lambda: "", raising=False)
    monkeypatch.setattr(api, "_settings", lambda: {}, raising=False)
    monkeypatch.setattr(api, "list_sessions", lambda: [], raising=False)
    return api.boot()


# --------------------------------------------------------------- the crash

def test_resuming_with_nothing_to_resume_does_not_raise():
    """The exact line from the traceback: `self._agent = None`.

    `_agent` is a property computed from the active chat, so assigning it is an
    AttributeError -- and this branch is the one taken when there are no stored
    chats at all, i.e. on every launch of a fresh install.
    """
    api = _api()
    api._cfg.last_session_id = ""
    assert api._resume_last() is None
    assert api.session_id is None
    assert api._agent is None          # the property still answers, via _active


def test_boot_survives_a_resume_that_blows_up(monkeypatch):
    """A convenience must not be able to stop the app starting.

    The window came up with no settings and no sessions because one failure
    inside _resume_last() propagated all the way out of boot().
    """
    cfg = config_mod.Config(setup_done=True, api_key="sk-live")
    api = _api(cfg)

    def explode():
        raise RuntimeError("corrupt session on disk")
    monkeypatch.setattr(api, "_resume_last", explode, raising=False)

    res = _boot(api, monkeypatch)
    assert res["session"] is None       # the chat is lost...
    assert res["needsKey"] is False     # ...but the app is not
    assert "settings" in res and "version" in res


# ------------------------------------------------- first-run detection

def test_a_key_left_in_the_environment_does_not_count_as_being_set_up(monkeypatch):
    """The heart of it. Config is gone; the registry key is not."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-from-the-last-install")
    cfg = config_mod.Config()           # what a fresh install starts with
    assert cfg.setup_done is False
    api = _api(cfg)

    res = _boot(api, monkeypatch)
    assert res["needsKey"] is True, "a leftover env var is not consent to skip setup"


def test_a_finished_setup_is_not_asked_again(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "sk-live")
    api = _api(config_mod.Config(setup_done=True))
    assert _boot(api, monkeypatch)["needsKey"] is False


def test_setup_reappears_if_the_key_is_taken_away(monkeypatch):
    """Set up once, but nothing to talk to now -- asking again is the only
    useful thing the app can do."""
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("MNM_API_KEY", raising=False)
    api = _api(config_mod.Config(setup_done=True, api_key=""))
    assert _boot(api, monkeypatch)["needsKey"] is True


def test_setup_is_not_resumed_over(monkeypatch):
    """Showing setup and resuming a chat behind it are mutually exclusive.

    Both fired together before: the key was found, so a session was restored,
    while the sheet on top asked for a key that was already in use.
    """
    monkeypatch.setenv("ZAI_API_KEY", "sk-leftover")
    api = _api(config_mod.Config())
    called = []
    monkeypatch.setattr(api, "_resume_last",
                        lambda: called.append(1), raising=False)
    res = _boot(api, monkeypatch)
    assert res["needsKey"] is True
    assert called == [], "resumed a chat while asking for first-run setup"


# ------------------------------------------------------------ upgrades

def test_an_existing_install_is_not_dragged_back_through_setup(tmp_path, monkeypatch):
    """Grandfathering. Every config written before setup_done existed belongs
    to someone who has already been through setup -- there was no other way to
    get one. Re-asking all of them would be worse than the bug being fixed."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text('{"model": "glm-4.7-flash"}')

    assert config_mod.load_config().setup_done is True


def test_no_config_file_at_all_means_a_fresh_install(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    assert config_mod.load_config().setup_done is False


def test_setup_done_is_written_and_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config.json")
    cfg = config_mod.Config(setup_done=True)
    config_mod.save_config(cfg)
    assert config_mod.load_config().setup_done is True


# ------------------------------------------- reusing a key already present

def test_setup_offers_the_key_this_pc_already_has(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google-from-before")
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("MNM_API_KEY", raising=False)
    api = _api(config_mod.Config())

    found = api.provider_choices()["found"]
    assert [f["preset"] for f in found] == ["google"]
    assert found[0]["env_var"] == "GOOGLE_API_KEY"


def test_the_key_itself_never_reaches_the_page(monkeypatch):
    """Names and presence only. The value is already in the process; nothing on
    screen needs it, and what never reaches the DOM cannot reach a screenshot."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-secret-value")
    api = _api(config_mod.Config())
    assert "sk-secret-value" not in repr(api.provider_choices())


def test_an_empty_box_is_accepted_when_the_key_is_already_here(monkeypatch):
    """Otherwise setup sends you to the registry to copy out a key the app can
    read perfectly well by itself."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-already-here")
    api = _api(config_mod.Config())
    monkeypatch.setattr(api, "_resume_last", lambda: None, raising=False)
    monkeypatch.setattr(api, "list_sessions", lambda: [], raising=False)

    res = api.save_setup("zai", "")
    assert res.get("ok") is True
    assert res["reused"] is True
    assert api._cfg.setup_done is True


def test_an_empty_box_is_still_refused_with_no_key_anywhere(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    api = _api(config_mod.Config())
    res = api.save_setup("zai", "")
    assert "error" in res
    assert api._cfg.setup_done is False, "a rejected setup must not count as done"


# ------------------------------------------------------- a keyless provider
#
# Ollama is the only preset that takes no key, which makes it the one that can
# be handed somebody else's.

def test_ollama_does_not_inherit_another_providers_key(monkeypatch):
    """provider_env_var() used to end in `or "ZAI_API_KEY"`, so a preset with
    no variable of its own read z.ai's -- and sent it to a server on this
    machine."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-secret")
    cfg = config_mod.Config(provider_preset="ollama",
                            base_url="http://localhost:11434/v1", api_key="local")
    assert cfg.provider_env_var() == ""
    assert cfg.resolve_api_key() == "local"


def test_setting_up_ollama_clears_a_previous_providers_stored_key(monkeypatch):
    """cfg.api_key is the "setx was blocked" fallback. Left alone it would
    still hold the hosted key from an earlier setup, and resolve_api_key()
    returns it when there is no environment variable -- which is always, here."""
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    api = _api(config_mod.Config(provider_preset="zai", api_key="sk-zai-secret"))
    monkeypatch.setattr(api, "_resume_last", lambda: None, raising=False)
    monkeypatch.setattr(api, "list_sessions", lambda: [], raising=False)

    res = api.save_setup("ollama", "", model="qwen2.5-coder")
    assert res.get("ok") is True
    assert api._cfg.api_key == "local"
    assert api._cfg.resolve_api_key() != "sk-zai-secret"


def test_ollama_setup_needs_a_model_but_not_a_key(monkeypatch):
    api = _api(config_mod.Config())
    monkeypatch.setattr(api, "_resume_last", lambda: None, raising=False)
    monkeypatch.setattr(api, "list_sessions", lambda: [], raising=False)

    # No model chosen: the preset names none, so there is nothing to fall back on.
    assert "error" in api.save_setup("ollama", "")
    assert api._cfg.setup_done is False

    res = api.save_setup("ollama", "", model="qwen2.5-coder:7b")
    assert res.get("ok") is True
    assert api._cfg.model == "qwen2.5-coder:7b"
    assert api._cfg.vision_model == "qwen2.5-coder:7b"
    assert api._cfg.base_url == "http://localhost:11434/v1"


def test_local_models_reports_the_three_states_apart(monkeypatch):
    """Not running, running-but-empty and ready need different fixes, so they
    cannot collapse into one "it didn't work"."""
    import requests

    def reply(payload):
        return lambda *a, **k: types.SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(requests, "get", reply({"models": [
        {"name": "qwen2.5-coder"}, {"name": "llama3"}]}))
    got = gui_app.Api.local_models("ollama")
    assert got == {"running": True, "models": ["llama3", "qwen2.5-coder"]}

    monkeypatch.setattr(requests, "get", reply({"models": []}))
    assert gui_app.Api.local_models("ollama") == {"running": True, "models": []}

    def refused(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(requests, "get", refused)
    assert gui_app.Api.local_models("ollama") == {"running": False, "models": []}


def test_local_models_refuses_to_probe_a_hosted_provider():
    """It builds a URL from the preset and fetches it. Anything not on this
    machine has no business being reached from here."""
    assert "error" in gui_app.Api.local_models("google")
    assert "error" in gui_app.Api.local_models("nonsense")
