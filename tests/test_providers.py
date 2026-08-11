"""Bring-your-own-model: provider config, per-chat model override, and the
vision-client split for custom providers."""

import json
import sys
import types

import glmcode.config as config
from glmcode.config import (BUILTIN_PROVIDER_NAME, Config, all_providers,
                            builtin_provider_name,
                            builtin_provider, find_provider, load_config,
                            save_config)
from glmcode.sessions import SessionStore

from conftest import FakeResult, tool_call

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
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
    # Addressed by its old hardcoded label, which is what a config written
    # before presets still shows in the row the form was opened from.
    res = api.save_provider(BUILTIN_PROVIDER_NAME, BUILTIN_PROVIDER_NAME,
                            "", "zk-123", "")
    assert "error" not in res
    assert persisted == {"ZAI_API_KEY": "zk-123"}
    assert api._cfg.api_key == "zk-123"
    assert res["persisted_env"] is True
    # no custom provider row was created for the builtin
    assert api._cfg.providers == []
    # and an empty key is refused
    assert "error" in api.save_provider(BUILTIN_PROVIDER_NAME,
                                        BUILTIN_PROVIDER_NAME, "", "", "")


def test_saving_the_primary_row_uses_that_providers_own_env_var(monkeypatch):
    """Configuring Google must not overwrite a z.ai key, and vice versa."""
    api = make_api(monkeypatch)
    api._cfg.provider_preset = "google"
    api._cfg.base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
    persisted = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: persisted.setdefault(n, v) or True)
    res = api.save_provider("Google AI Studio", "Google AI Studio", "", "g-key", "")
    assert "error" not in res
    assert persisted == {"GOOGLE_API_KEY": "g-key"}
    assert api._cfg.providers == [], "the primary provider is not a custom row"


def test_builtin_provider_always_first():
    cfg = Config()
    provs = all_providers(cfg)
    # Named after what it actually is, rather than a vendor label that used to
    # be printed whatever the base URL pointed at.
    assert provs[0]["name"] == builtin_provider_name(cfg)
    assert provs[0]["builtin"] is True
    assert cfg.model in provs[0]["models"]


def test_find_provider():
    cfg = Config(providers=[{"name": "OpenRouter", "base_url": "https://x/v1",
                             "api_key": "k", "models": ["m1"]}])
    assert find_provider(cfg, "OpenRouter")["base_url"] == "https://x/v1"
    assert find_provider(cfg, builtin_provider_name(cfg))["builtin"] is True
    # Chats saved before presets name the primary provider the old way; they
    # must keep resolving, or they would quietly switch model on next open.
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


def test_thinking_only_sent_to_builtin_not_byom(scripted_agent):
    """GLM's z.ai-specific `thinking` param must never be sent to a custom
    BYOM endpoint (Ollama/OpenRouter would reject or choke on it)."""
    agent = scripted_agent()
    agent.cfg.thinking = True
    seen = []
    orig = agent.client.chat

    def spy(**kw):
        seen.append((kw.get("model"), kw.get("thinking")))
        return orig(**kw)

    agent.client.chat = spy

    # built-in chat (no override): thinking is sent
    agent.model_override = None
    agent.run_turn({"role": "user", "content": "hi"})
    assert seen and seen[-1][1] is True

    # custom model: thinking must be withheld
    seen.clear()
    agent.model_override = "custom/coder"
    agent.run_turn({"role": "user", "content": "hi"})
    assert seen and all(t is False for _, t in seen)


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


# --------------------------------------------------------------------- #
# Whose key goes where.
#
# ZAI_API_KEY used to be consulted for EVERY provider, after the preset's own
# variable and before the stored key. That is one provider's credential being
# offered to another, and it is reachable two ways that both look ordinary.

def test_a_typed_in_endpoint_is_not_given_the_zai_key(monkeypatch):
    """Pick "Other", point it at a local server, leave the key box empty --
    which a local server invites you to do -- and the z.ai key was sent there."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-secret")
    monkeypatch.delenv("MNM_API_KEY", raising=False)
    cfg = config.Config(provider_preset="custom",
                        base_url="http://localhost:11434/v1", api_key="")
    assert cfg.resolve_api_key() == ""


def test_google_does_not_fall_back_to_the_zai_key(monkeypatch):
    """The locked-down-machine path: `setx` is blocked by policy, so
    GOOGLE_API_KEY never persists. The stored api_key is the right answer and
    this fallback used to win ahead of it."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-secret")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    cfg = config.Config(provider_preset="google", api_key="sk-google-stored")
    assert cfg.resolve_api_key() == "sk-google-stored"


def test_an_install_predating_presets_still_finds_its_key(monkeypatch):
    """Why the fallback exists at all: no preset was ever chosen, and the key
    is sitting in ZAI_API_KEY under a name no migration could reach."""
    monkeypatch.setenv("ZAI_API_KEY", "sk-from-2025")
    cfg = config.Config(provider_preset="", base_url="", api_key="")
    assert cfg.resolve_api_key() == "sk-from-2025"


def test_a_chosen_provider_still_prefers_its_own_variable(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai")
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-google")
    cfg = config.Config(provider_preset="google")
    assert cfg.resolve_api_key() == "sk-google"
