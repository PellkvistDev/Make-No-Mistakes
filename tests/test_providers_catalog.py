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
        # https for anything hosted. A server on this machine is the exception
        # and the only one: Ollama speaks plain http on localhost, and there is
        # no certificate it could present for it.
        if providers.is_local(p["base_url"]):
            assert p["base_url"].startswith("http://"), p["key"]
        else:
            assert p["base_url"].startswith("https://"), p["key"]
        # A preset normally names the model it starts on. A local one cannot:
        # which models exist depends on what has been pulled onto the machine,
        # so it starts on nothing and reads the list off the running server.
        if p["model"]:
            assert p["model"] in p["models"], \
                f"{p['key']} starts on a model it does not list"
        else:
            assert p.get("needs_key") is False, \
                f"{p['key']} names no model but is not a local server"
            assert p.get("suggest_pull"), \
                f"{p['key']} has no model and nothing to suggest pulling"
        assert p["key_url"].startswith("https://"), p["key"]
        assert p["steps"], f"{p['key']} tells you nothing about getting a key"
        # Somewhere to put a key, or an explicit statement that it needs none.
        assert providers.env_var_for(p["key"]) or p.get("needs_key") is False, \
            f"{p['key']} has nowhere to put its key"


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
    pointed at something else entirely."""
    name = cfgmod.builtin_provider_name(_cfg(base_url="http://127.0.0.1:9999/v1"))
    assert "z.ai" not in name.lower()
    assert "127.0.0.1:9999" in name


def test_ollamas_own_url_is_recognised_rather_than_shown_as_a_host():
    """It used to come out as the bare "localhost:11434", because a local
    server was only ever something you typed in yourself."""
    name = cfgmod.builtin_provider_name(_cfg(base_url="http://localhost:11434/v1"))
    assert name == "Ollama (on this PC)"


def test_a_chat_saved_before_ollama_was_a_preset_still_resolves():
    """That rename would otherwise strand chats saved in between: the stored
    provider name no longer matches anything, and the chat silently falls back
    to the default model mid-conversation."""
    cfg = _cfg(base_url="http://localhost:11434/v1")
    found = cfgmod.find_provider(cfg, "localhost:11434")
    assert found is not None
    assert found["base_url"] == "http://localhost:11434/v1"


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


def test_a_model_whose_price_is_unknown_is_not_claimed_to_be_free():
    """Whether Google's free tier covers Pro has changed more than once: 5 RPM
    and 100 RPD in early 2026, reported behind billing since. A file in this
    repo cannot stay right about that, and a confident wrong label is worse
    than no label -- so the third state exists and Pro is in it."""
    g = next(c for c in providers.choices() if c["key"] == "google")
    by_name = {m["name"]: m["tier"] for m in g["model_options"]}
    assert by_name["gemini-2.5-flash"] == "free"
    assert by_name["gemini-2.5-pro"] == "unsure"


def test_pro_is_still_offered_as_a_model():
    """Uncertain about the price is not a reason to hide the better model."""
    assert "gemini-2.5-pro" in providers.preset("google")["models"]


# -- what a model costs, and when to say nothing ------------------------- #

def test_a_hand_typed_endpoint_gets_no_price_claim():
    """The bug this whole tier exists to prevent. The app printed "$0.00" and
    "via z.ai" from constants in the markup -- fine while z.ai was the only
    thing it could talk to, and a claim about a stranger's billing the moment
    it could talk to anything else."""
    assert providers.model_tier("https://openrouter.ai/api/v1", "gpt-4o") == ""


def test_a_model_running_on_this_machine_is_known_to_be_free():
    """Not a free tier -- there is no quota, no account, and no policy that
    can change next month. That is worth saying, and it is knowable."""
    for url in ("http://localhost:11434/v1", "http://127.0.0.1:1234/v1",
                "http://[::1]:8080/v1"):
        assert providers.model_tier(url, "qwen2.5-coder") == "local", url


def test_a_remote_host_is_never_mistaken_for_a_local_one():
    """Substring matching on "localhost" would call this local. It is not."""
    assert not providers.is_local("https://localhost.evil.example.com/v1")
    assert not providers.is_local("https://api.z.ai/api/paas/v4")


def test_the_free_tier_is_only_claimed_for_the_models_it_covers():
    g = providers.preset("google")["base_url"]
    assert providers.model_tier(g, "gemini-2.5-flash") == "free"
    assert providers.model_tier(g, "gemini-2.5-pro") == "unsure"
    # A Gemini model the catalogue has never heard of: no claim either way.
    assert providers.model_tier(g, "gemini-9.9-ultra") == ""


def test_a_trailing_slash_does_not_lose_the_provider():
    """Base URLs are stored rstrip'd in some paths and not others."""
    g = providers.preset("google")["base_url"]
    assert providers.model_tier(g + "/", "gemini-2.5-flash") == "free"
