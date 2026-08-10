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
