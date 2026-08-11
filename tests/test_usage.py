"""Counting requests per model per day.

The free tiers that matter are metered in requests per DAY, and the numbers are
small enough to run out inside one task -- Google's free tier gives Gemini 3.6
Flash twenty a day, and an agentic turn spends several. The quota page that
prompted this showed 23/20 already used.

Nothing here asks a provider. No endpoint reports free-tier consumption, so
this counts what we sent, which is the only figure the app knows first-hand.
"""

import json
import sys
import types

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

import pytest

from glmcode import providers, usage


@pytest.fixture(autouse=True)
def tmp_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(usage, "USAGE_FILE", tmp_path / "model_usage.json")
    yield


def test_requests_are_counted_per_model():
    usage.record("gemini-3.6-flash")
    usage.record("gemini-3.6-flash")
    usage.record("gemini-3.5-flash-lite")
    assert usage.today() == {"gemini-3.6-flash": 2, "gemini-3.5-flash-lite": 1}


def test_a_new_day_starts_from_zero(monkeypatch, tmp_path):
    usage.record("m")
    stale = json.loads((tmp_path / "model_usage.json").read_text())
    stale["day"] = "2000-01-01"
    (tmp_path / "model_usage.json").write_text(json.dumps(stale))
    assert usage.today() == {}


def test_a_corrupt_counter_file_is_not_fatal(tmp_path):
    (tmp_path / "model_usage.json").write_text("{not json")
    assert usage.today() == {}
    usage.record("m")                 # and recovers
    assert usage.today() == {"m": 1}


def test_counting_never_raises(monkeypatch):
    """A counter must not be able to break a turn."""
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(usage, "_read", boom)
    usage.record("m")                 # no exception, and the turn goes on
    assert usage.today() == {}        # nor does reading it back


# ---- what the quota page actually said ------------------------------------

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"


def test_no_pro_model_is_offered_on_a_tier_that_has_none():
    """Both Pro models read 0 / 0 on the free quota page -- no access at all.
    That is the answer to a question this catalogue spent a long time hedging,
    and it came from an account rather than from documentation."""
    chat = providers.chat_models(GOOGLE)
    assert not any("pro" in m for m in chat), chat


def test_the_default_is_a_model_that_can_survive_a_task():
    """Flash gets 20 requests a day and an agentic turn spends several, so it
    runs out inside one job. A weaker model that answers beats a better one
    that 429s -- and the picker shows both numbers so the trade is visible."""
    default = providers.preset("google")["model"]
    assert providers.free_limits(GOOGLE, default)["rpd"] >= 500


def test_every_offered_model_has_a_free_allowance():
    """A model with no free-tier quota does not belong in a list a free key
    picks from -- it fails on selection with no explanation."""
    for m in providers.chat_models(GOOGLE):
        assert providers.free_limits(GOOGLE, m), m


def test_an_unknown_model_gets_no_claimed_limit():
    """None means "no figure", not "unlimited" -- the UI must not draw a full
    ring for it."""
    assert providers.free_limits(GOOGLE, "gemini-99-turbo") is None
    assert providers.free_limits("https://example.test/v1", "anything") is None


def test_gemma_is_selectable_even_though_it_is_not_recommended():
    """Gemma was filtered out twice, on two wrong reasons: that it 404s on an
    ordinary key (a quota page says 30 rpm / 14,400 rpd), and that it cannot do
    tool calling -- which confused the model with one way of serving it. Gemma
    runs tools fine under a local runner.

    Whether Google's hosted endpoint accepts a tools array for it is a separate
    question nobody here has measured. So it is reachable and simply not
    recommended: trying it settles the question, hiding it guarantees nobody
    ever can.
    """
    assert providers.is_chat_model("gemma-4-31b-it")
    # ...but not on the default menu, and not something the app would pick.
    assert not any("gemma" in m for m in providers.chat_models(GOOGLE))
    assert "gemma" not in providers.preset("google")["model"]


def test_things_that_are_not_chat_models_stay_out():
    """Loosening the filter for Gemma must not let image or speech models in --
    those fail on the first request, which is a different thing from untested."""
    for junk in ("imagen-4-generate", "veo-3-fast", "gemini-2.5-flash-tts",
                 "text-embedding-004", "gemini-3-flash-live"):
        assert not providers.is_chat_model(junk), junk


def test_gemma_reaches_the_picker_only_under_show_all():
    """The two questions are different and were tangled: "can you chat with
    it" (yes -- it runs tools under a local runner) and "should the app suggest
    it" (not until Google's hosted endpoint is known to accept tool calls)."""
    listing = ["gemini-3.5-flash", "gemma-4-31b-it"]
    assert providers.shortlist(listing) == ["gemini-3.5-flash"]
    assert "gemma-4-31b-it" in [m for m in listing if providers.is_chat_model(m)]


def test_a_provider_offering_only_unrecommended_models_still_offers_them():
    """Better a menu of the untested than an empty one -- the same rule the
    preview-only case needed."""
    assert providers.shortlist(["gemma-4-31b-it"]) == ["gemma-4-31b-it"]
