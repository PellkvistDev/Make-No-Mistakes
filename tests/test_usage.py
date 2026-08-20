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


# ---- refusals, which are the only first-hand number here -------------------
#
# `today()` is a count measured against providers.free_limits(), a table in
# this repository. It is a best effort: it goes stale when a tier changes and
# has no row at all for most models. A 429 is the provider itself saying no.

def test_refusals_are_counted_per_model():
    usage.record_limited("gemini-3.6-flash")
    usage.record_limited("gemini-3.6-flash")
    usage.record_limited("gemini-3.5-flash")
    got = usage.limited_today()
    assert got["gemini-3.6-flash"]["n"] == 2
    assert got["gemini-3.5-flash"]["n"] == 1


def test_the_last_refusal_is_stamped():
    """"11 times today" is history. "the last one was a minute ago" is what
    tells you the chain is being walked right now."""
    import time
    before = time.time()
    usage.record_limited("m")
    at = usage.limited_today()["m"]["at"]
    assert before <= at <= time.time() + 1


def test_refusals_and_requests_are_kept_together():
    """Read side by side on purpose: "4 of 20 used" next to "refused 11 times"
    says plainly that the table is wrong, which is the one thing the count on
    its own can never say."""
    usage.record("m")
    usage.record_limited("m")
    assert usage.today() == {"m": 1}
    assert usage.limited_today()["m"]["n"] == 1


def test_a_new_day_clears_the_refusals_too():
    usage.record_limited("m")
    stale = json.loads(usage.USAGE_FILE.read_text())
    stale["day"] = "2000-01-01"
    usage.USAGE_FILE.write_text(json.dumps(stale))
    assert usage.limited_today() == {}


def test_a_counter_file_from_before_this_existed_still_reads():
    """`limited` was added after `counts`, so a file written by an older build
    has no such key. Defaulted rather than migrated -- the worst case of
    getting a counter wrong is one day of missing numbers."""
    usage.USAGE_FILE.write_text(json.dumps(
        {"day": usage._today(), "counts": {"m": 5}}))
    assert usage.today() == {"m": 5}
    assert usage.limited_today() == {}
    usage.record_limited("m")                      # and starts counting
    assert usage.limited_today()["m"]["n"] == 1
    assert usage.today() == {"m": 5}               # without losing the other half


def test_counting_a_refusal_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(usage, "_read", boom)
    usage.record_limited("m")
    assert usage.limited_today() == {}


def test_a_model_with_no_refusals_is_absent_rather_than_zero():
    """The UI decides whether to draw anything from presence. A row of zero
    would badge every model that has ever been asked."""
    usage.record("m")
    assert usage.limited_today() == {}


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


# ---- and what the UI is handed --------------------------------------------

def test_the_providers_payload_carries_the_refusals(monkeypatch):
    """The wiring, end to end. Everything above proves the counter works; this
    is whether the number ever reaches the screen -- which is the half that
    was missing when the model menu could only draw a fraction of a table."""
    from glmcode.config import load_config
    from glmcode.gui import app as gui_app

    cfg = load_config()
    google = "https://generativelanguage.googleapis.com/v1beta/openai"
    model = providers.chat_models(google)[0]
    usage.record(model)
    usage.record_limited(model)
    usage.record_limited(model)
    monkeypatch.setattr(gui_app, "all_providers", lambda c: [
        {"name": "Google", "base_url": google, "models": [model],
         "all_models": [model]}])

    stand_in = types.SimpleNamespace(_cfg=cfg, session_provider="",
                                     session_model="")
    q = gui_app.Api.providers(stand_in)["providers"][0]["quota"][model]
    assert q["used"] == 1
    assert q["limited"] == 2
    assert q["limited_at"] > 0
    assert q["cooling"] is False        # nothing has been asked, so nothing rests


def test_a_model_refused_moments_ago_is_reported_as_resting(monkeypatch):
    """"Why did it switch models on me" is answered by `cooling`, not by the
    count: a chain that walked past this model an hour ago and one walking past
    it right now have the same tally."""
    from glmcode import api
    from glmcode.config import load_config
    from glmcode.gui import app as gui_app

    cfg = load_config()
    google = "https://generativelanguage.googleapis.com/v1beta/openai"
    model = providers.chat_models(google)[0]
    api.clear_cooldowns()
    try:
        api.note_rate_limited(google, model)
        monkeypatch.setattr(gui_app, "all_providers", lambda c: [
            {"name": "Google", "base_url": google, "models": [model],
             "all_models": [model]}])
        stand_in = types.SimpleNamespace(_cfg=cfg, session_provider="",
                                         session_model="")
        q = gui_app.Api.providers(stand_in)["providers"][0]["quota"][model]
        assert q["cooling"] is True
        assert q["limited"] == 1        # note_rate_limited counts it too
    finally:
        api.clear_cooldowns()
