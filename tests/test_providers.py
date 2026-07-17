"""Bring-your-own-model: provider config, per-chat model override, and the
vision-client split for custom providers."""

import json
import sys
import types

import glmcode.config as config
from glmcode.config import (BUILTIN_PROVIDER_NAME, Config, all_providers,
                            builtin_provider, find_provider, load_config,
                            save_config)
from glmcode.sessions import SessionStore

from conftest import FakeResult, tool_call

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402


def make_api(monkeypatch):
    """A minimal Api for the provider CRUD paths: real Config, no window,
    no disk writes, no setx."""
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = Config()
    api._chats = {}
    api.session_id = None
    api._client = None
    monkeypatch.setattr(gui_app, "save_config", lambda cfg: None)
    return api


def test_save_provider_adds_then_edits_in_place(monkeypatch):
    api = make_api(monkeypatch)
    res = api.save_provider("", "OpenRouter", "https://openrouter.ai/api/v1/",
                            "sk-x", "m1, m2")
    assert "error" not in res
    p = api._cfg.providers[0]
    assert p["base_url"] == "https://openrouter.ai/api/v1"  # trailing / stripped
    assert p["models"] == ["m1", "m2"]

    # Edit: rename it, trim the models, leave the key field empty -> the
    # stored key survives.
    res = api.save_provider("OpenRouter", "OR", "https://openrouter.ai/api/v1",
                            "", "m1")
    assert "error" not in res
    p = api._cfg.providers[0]
    assert (p["name"], p["api_key"], p["models"]) == ("OR", "sk-x", ["m1"])


def test_save_provider_validation(monkeypatch):
    api = make_api(monkeypatch)
    api.save_provider("", "A", "https://a/v1", "k", "m")
    assert "already exists" in api.save_provider("", "A", "https://b/v1", "", "m")["error"]
    assert "required" in api.save_provider("", "B", "", "", "")["error"]
    assert "to edit" in api.save_provider("ghost", "G", "https://g/v1", "", "m")["error"]
    # renaming one custom API onto another's name is also a clash
    api.save_provider("", "B", "https://b/v1", "", "m")
    assert "already exists" in api.save_provider("B", "A", "https://b/v1", "", "m")["error"]


def test_saving_builtin_row_sets_env_key(monkeypatch):
    api = make_api(monkeypatch)
    persisted = {}

    def fake_persist(name, value):
        persisted[name] = value
        return True

    monkeypatch.setattr(gui_app, "persist_env_var", fake_persist)
    res = api.save_provider(BUILTIN_PROVIDER_NAME, BUILTIN_PROVIDER_NAME,
                            "", "zk-123", "")
    assert "error" not in res
    assert persisted == {"ZAI_API_KEY": "zk-123"}
    assert api._cfg.api_key == "zk-123"
    assert res["persisted_env"] is True
    # no custom provider row was created for the builtin
    assert api._cfg.providers == []
    # and an empty key is refused with a pointer at where to get one
    assert "z.ai" in api.save_provider(BUILTIN_PROVIDER_NAME,
                                       BUILTIN_PROVIDER_NAME, "", "", "")["error"]


def test_builtin_provider_always_first():
    cfg = Config()
    provs = all_providers(cfg)
    assert provs[0]["name"] == BUILTIN_PROVIDER_NAME
    assert provs[0]["builtin"] is True
    assert cfg.model in provs[0]["models"]


def test_find_provider():
    cfg = Config(providers=[{"name": "OpenRouter", "base_url": "https://x/v1",
                             "api_key": "k", "models": ["m1"]}])
    assert find_provider(cfg, "OpenRouter")["base_url"] == "https://x/v1"
    assert find_provider(cfg, BUILTIN_PROVIDER_NAME)["builtin"] is True
    assert find_provider(cfg, "nope") is None


def test_providers_roundtrip_through_config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    cfg = Config(providers=[{"name": "Local", "base_url": "http://l/v1",
                             "api_key": "", "models": ["a", "b"]}])
    save_config(cfg)
    loaded = load_config()
    assert loaded.providers == cfg.providers


def test_session_stores_model_choice(tmp_path):
    store = SessionStore(root=tmp_path)
    store.save("s1", "/proj", [{"role": "user", "content": "hi"}], 1, 1,
               model_provider="Ollama (local)", model="llama3:8b")
    data = store.load("s1")
    assert data["model_provider"] == "Ollama (local)"
    assert data["model"] == "llama3:8b"


def test_agent_uses_model_override(scripted_agent):
    seen = {}

    def script(n):
        return FakeResult(content="hi")

    agent = scripted_agent(script)
    orig_chat = agent.client.chat

    def spy(**kwargs):
        seen["model"] = kwargs.get("model")
        return orig_chat(**kwargs)

    agent.client.chat = spy
    agent.model_override = "custom/model-x"
    agent.run_turn({"role": "user", "content": "q"})
    assert seen["model"] == "custom/model-x"


def test_client_for_routes_vision_to_vision_client(scripted_agent):
    agent = scripted_agent()
    other = object()
    agent.vision_client = other
    assert agent._client_for(agent.cfg.vision_model) is other
    assert agent._client_for("anything-else") is agent.client
    agent.vision_client = None
    assert agent._client_for(agent.cfg.vision_model) is agent.client


def test_subagent_inherits_model_override(scripted_agent):
    from conftest import ScriptedClient
    coord = scripted_agent(allow_subagents=True)
    coord.model_override = "custom/model-x"
    seen = []

    def sub_script(n):
        return FakeResult(content="report")

    ScriptedClient.scripts = [sub_script]
    orig_init = ScriptedClient.__init__

    coord._run_subagents([{"name": "w", "task": "t"}])
    # the coordinator's report path worked; verify the override reached the
    # sub-agent by checking the recorded transcript of models isn't possible
    # via ScriptedClient (it ignores model), so assert via a fresh sub run:
    # simplest -- the propagation line itself:
    # (covered indirectly; direct check below)
    import glmcode.agent as agent_mod
    sub_holder = {}
    real_run = agent_mod.Agent.run_turn

    def capture_run(self, msg):
        sub_holder["override"] = self.model_override
        return real_run(self, msg)

    ScriptedClient.scripts = [sub_script]
    agent_mod.Agent.run_turn = capture_run
    try:
        coord._run_subagents([{"name": "w", "task": "t"}])
    finally:
        agent_mod.Agent.run_turn = real_run
    assert sub_holder["override"] == "custom/model-x"
