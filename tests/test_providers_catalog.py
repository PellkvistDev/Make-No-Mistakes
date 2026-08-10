"""The provider catalogue: presets, and the config plumbing under them.

z.ai used to be wired in as *the* provider -- a DEFAULT_BASE_URL, a ZAI_API_KEY
environment variable, and a provider hardcoded to the name "z.ai (free)"
whatever it actually pointed at. These cover the two things that had to become
true instead: that a preset is only a label and some instructions, and that
adding a second one cannot break the first.
"""

import types

from glmcode import config as cfgmod
from glmcode import providers


def _cfg(**kw):
    c = cfgmod.Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ---- the presets themselves ------------------------------------------------

def test_every_preset_can_actually_be_used():
    """A preset that is missing any of these is not a shortcut, it is a dead
    end the user has to work around."""
    for p in providers.PRESETS:
        assert p["key"] and p["label"]
        assert p["base_url"].startswith("https://"), p["key"]
        assert p["model"] in p["models"], f"{p['key']} starts on a model it does not list"
        assert p["key_url"].startswith("https://"), p["key"]
        assert p["steps"], f"{p['key']} tells you nothing about getting a key"
        assert providers.env_var_for(p["key"]), f"{p['key']} has nowhere to put its key"


def test_presets_do_not_share_an_environment_variable():
    """Sharing one would mean configuring the second provider silently
    overwrites the first one's key."""
    names = [providers.env_var_for(p["key"]) for p in providers.PRESETS]
    assert len(set(names)) == len(names)


def test_google_points_at_the_openai_compatible_endpoint():
    """The native generateContent API is a different protocol and would need a
    client of its own; this app only speaks /chat/completions."""
    g = providers.preset("google")
    assert g["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_google_says_plainly_that_free_prompts_train_the_model():
    """The one thing about this option someone might mind, and would otherwise
    find out afterwards. This app sends source code."""
    g = providers.preset("google")
    assert g["caveat"], "the free-tier training caveat is missing"
    assert "free" in g["caveat"].lower()


def test_a_preset_becomes_an_ordinary_provider():
    """Nothing downstream should be able to tell a preset from a typed-in one."""
    prov = providers.to_provider("google", "KEY123")
    assert prov["base_url"] == providers.preset("google")["base_url"]
    assert prov["api_key"] == "KEY123"
    assert prov["models"] == providers.preset("google")["models"]
    assert set(prov) >= {"name", "base_url", "api_key", "models"}


def test_an_unknown_preset_is_none_rather_than_a_broken_provider():
    assert providers.to_provider("nope") is None
    assert providers.preset("nope") is None


def test_the_setup_screen_is_offered_every_preset_plus_other():
    keys = [c["key"] for c in providers.choices()]
    assert keys[:len(providers.PRESETS)] == providers.preset_keys()
    assert keys[-1] == providers.CUSTOM_KEY
    for c in providers.choices():
        assert c["label"] and c["blurb"] and c["steps"]


def test_a_base_url_is_recognised_as_its_preset():
    """Installs that predate presets have only a base_url. Without this they
    would all read as "custom" and lose their instructions."""
    assert providers.preset_from_base_url(
        "https://api.z.ai/api/paas/v4")["key"] == "zai"
    # Trailing slash and case are not meaningful in a URL people paste by hand.
    assert providers.preset_from_base_url(
        "https://API.Z.AI/api/paas/v4/")["key"] == "zai"
    assert providers.preset_from_base_url("https://example.invalid/v1") is None
    assert providers.preset_from_base_url("") is None


# ---- the config plumbing ---------------------------------------------------

def test_the_primary_provider_is_named_after_what_it_actually_is():
    assert cfgmod.builtin_provider_name(_cfg(provider_preset="google")) == "Google AI Studio"
    assert cfgmod.builtin_provider_name(_cfg(provider_preset="zai")) == "Z.AI"


def test_an_old_config_is_named_from_its_base_url():
    """No provider_preset, because it was written before presets existed."""
    assert cfgmod.builtin_provider_name(
        _cfg(base_url="https://api.z.ai/api/paas/v4")) == "Z.AI"


def test_a_hand_typed_provider_is_not_labelled_as_a_vendor_it_is_not():
    """The old code called every primary provider "z.ai (free)", including one
    pointed at a local Ollama."""
    name = cfgmod.builtin_provider_name(_cfg(base_url="http://localhost:11434/v1"))
    assert "z.ai" not in name.lower()
    assert "localhost:11434" in name


def test_each_preset_reads_its_own_key(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    assert _cfg(provider_preset="zai").resolve_api_key() == "zai-key"
    assert _cfg(provider_preset="google").resolve_api_key() == "google-key"


def test_an_existing_key_still_works_after_presets_arrive(monkeypatch):
    """The key is in an environment variable under the old name and no
    migration can reach it, so the old name has to keep resolving."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("ZAI_API_KEY", "the-old-key")
    assert _cfg().resolve_api_key() == "the-old-key"
    assert _cfg(provider_preset="zai").resolve_api_key() == "the-old-key"


def test_the_config_file_key_is_still_the_last_resort(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert _cfg(api_key="from-file").resolve_api_key() == "from-file"


def test_the_primary_provider_carries_its_preset(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    prov = cfgmod.builtin_provider(_cfg(provider_preset="google"))
    assert prov["preset"] == "google"
    assert prov["api_key"] == "g"
    assert prov["builtin"] is True


# ---- first-run setup -------------------------------------------------------

def _api(monkeypatch, **cfg_kw):
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.gui import app as gui_app
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = _cfg(**cfg_kw)
    api._client = "sentinel"
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    monkeypatch.setattr(gui_app.Api, "_resume_last", lambda self: None)
    monkeypatch.setattr(gui_app.Api, "list_sessions", lambda self: [])
    return api, gui_app


def test_setup_offers_the_catalogue_rather_than_a_hardcoded_screen(monkeypatch):
    api, _ = _api(monkeypatch)
    keys = [c["key"] for c in api.provider_choices()["choices"]]
    assert "zai" in keys and "google" in keys and keys[-1] == providers.CUSTOM_KEY


def test_choosing_google_configures_it_and_stores_the_key_in_its_own_var(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    res = api.save_setup("google", "g-key")
    assert "error" not in res
    assert api._cfg.provider_preset == "google"
    assert api._cfg.base_url == providers.preset("google")["base_url"]
    assert api._cfg.model == providers.preset("google")["model"]
    assert seen == {"GOOGLE_API_KEY": "g-key"}
    assert res["provider"] == "Google AI Studio"


def test_choosing_zai_does_not_reach_for_googles_variable(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    api.save_setup("zai", "z-key")
    assert seen == {"ZAI_API_KEY": "z-key"}
    assert api._cfg.provider_preset == "zai"


def test_other_needs_a_url_and_a_model_because_nothing_can_guess_them(monkeypatch):
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    assert "error" in api.save_setup("custom", "k", "", "m")
    assert "error" in api.save_setup("custom", "k", "https://x/v1", "")


def test_other_works_with_no_key_at_all(monkeypatch):
    """Ollama and LM Studio have no key. Demanding one makes the app unusable
    with the very setups "Other" exists for."""
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    res = api.save_setup("custom", "", "http://localhost:11434/v1/", "llama3")
    assert "error" not in res
    assert api._cfg.base_url == "http://localhost:11434/v1"   # trailing / trimmed
    assert api._cfg.model == "llama3"
    assert api._cfg.provider_preset == "custom"


def test_a_preset_still_insists_on_a_key(monkeypatch):
    api, gui_app = _api(monkeypatch)
    monkeypatch.setattr(gui_app, "persist_env_var", lambda n, v: True)
    assert "error" in api.save_setup("google", "")


def test_a_hand_typed_endpoint_does_not_store_its_key_as_a_zai_key(monkeypatch):
    api, gui_app = _api(monkeypatch)
    seen = {}
    monkeypatch.setattr(gui_app, "persist_env_var",
                        lambda n, v: seen.setdefault(n, v) or True)
    api.save_setup("custom", "sk-abc", "https://openrouter.ai/api/v1", "some/model")
    assert list(seen) == ["MNM_API_KEY"], seen


def test_setup_completes_even_when_the_environment_cannot_be_written(monkeypatch):
    """The long-standing rule for this screen: a locked-down machine must still
    end up with a working app, because the key is live in os.environ anyway."""
    api, gui_app = _api(monkeypatch)

    def blows_up(name, value):
        raise OSError("setx is blocked")

    monkeypatch.setattr(gui_app, "persist_env_var", blows_up)
    res = api.save_setup("google", "g-key")
    assert "error" not in res
    assert res["persisted"] is False
    assert api._cfg.api_key == "g-key", "no fallback source for the key"
