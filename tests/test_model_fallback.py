"""Falling back to another model when the preferred one is rate-limited.

The free tiers this app is built around are metered in requests per day and per
minute, and hitting the limit does not make you a worse programmer -- it just
stops you. A chain ("3.6 flash, then 3.5 flash, then 3.5 flash lite") turns a
hard stop into a slower answer.

What it costs, and why it is worth paying: this app re-sends ~12,400 tokens of
system prompt and tool schemas per request, and a different model has no prompt
cache for that prefix. But the alternative is not "keep the cache" -- it is
"get nothing". The history itself is unaffected: it is plain OpenAI-format
messages, and this app already switches models per chat.

What it must NOT do is quietly become a worse model for the rest of the
session, which is why the chain is walked only on a rate limit and the
preferred model is returned to as soon as its cooldown is up.
"""

import types

import pytest

from glmcode import api
from glmcode.api import ApiError, ZaiClient


@pytest.fixture(autouse=True)
def _clean():
    api.clear_cooldowns()
    yield
    api.clear_cooldowns()


def client(monkeypatch, script):
    """A client whose _stream_once follows a script of outcomes per model."""
    c = ZaiClient("k", "https://api.example/v1")
    asked = []

    def stream_once(payload, on_content, on_reasoning, cancel=None):
        model = payload["model"]
        asked.append(model)
        outcome = script.get(model, "ok")
        if outcome == "429":
            raise ApiError(429, "quota exceeded", retry_after=60)
        if outcome == "500":
            raise ApiError(500, "server error")
        return api.ChatResult(content=f"answered by {model}")

    monkeypatch.setattr(c, "_stream_once", stream_once)
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    c.asked = asked
    return c


def chat(c, model, fallbacks=None, **kw):
    return c.chat(model=model, messages=[{"role": "user", "content": "hi"}],
                  tools=None, temperature=0.5, max_tokens=100,
                  fallbacks=fallbacks, **kw)


# --------------------------------------------------------------------- #
# The chain

def test_the_preferred_model_is_used_when_it_works(monkeypatch):
    c = client(monkeypatch, {})
    out = chat(c, "flash-3.6", ["flash-3.5"])
    assert out.model == "flash-3.6"
    assert c.asked == ["flash-3.6"]


def test_a_rate_limit_moves_to_the_next_model(monkeypatch):
    c = client(monkeypatch, {"flash-3.6": "429"})
    out = chat(c, "flash-3.6", ["flash-3.5", "flash-lite"])
    assert out.model == "flash-3.5"
    assert c.asked == ["flash-3.6", "flash-3.5"]


def test_it_walks_the_whole_chain(monkeypatch):
    c = client(monkeypatch, {"flash-3.6": "429", "flash-3.5": "429"})
    out = chat(c, "flash-3.6", ["flash-3.5", "flash-lite"])
    assert out.model == "flash-lite"


def test_switching_does_not_sit_out_the_backoff(monkeypatch):
    """The wait is the thing the chain exists to avoid. Sleeping the full
    retry-after and THEN switching would give up the entire benefit."""
    slept = []
    monkeypatch.setattr(api.time, "sleep", lambda s: slept.append(s))
    c = client(monkeypatch, {"flash-3.6": "429"})
    chat(c, "flash-3.6", ["flash-3.5"])
    assert not [s for s in slept if s > 1.5], f"waited {slept} before switching"


def test_an_ordinary_error_does_not_switch_models(monkeypatch):
    """A weaker model is not the answer to a server fault or a bad request --
    only to being refused for quota."""
    c = client(monkeypatch, {"flash-3.6": "500"})
    with pytest.raises(ApiError):
        chat(c, "flash-3.6", ["flash-3.5"])
    assert set(c.asked) == {"flash-3.6"}, "a 500 fell back to another model"


def test_no_chain_means_the_old_behaviour_exactly(monkeypatch):
    c = client(monkeypatch, {"flash-3.6": "429"})
    with pytest.raises(ApiError):
        chat(c, "flash-3.6")
    assert set(c.asked) == {"flash-3.6"}


# --------------------------------------------------------------------- #
# Coming back

def test_a_rate_limited_model_is_skipped_next_time(monkeypatch):
    """Asking a model that just refused for quota, on every subsequent turn,
    spends a request to be told the same thing."""
    c = client(monkeypatch, {"flash-3.6": "429"})
    chat(c, "flash-3.6", ["flash-3.5"])
    c.asked.clear()
    out = chat(c, "flash-3.6", ["flash-3.5"])
    assert out.model == "flash-3.5"
    assert c.asked == ["flash-3.5"], "it asked the rate-limited model again"


def test_it_goes_back_once_the_cooldown_is_up(monkeypatch):
    """A per-minute limit recovers in a minute. Being exiled to the weakest
    model for the rest of the session is the failure this must not have."""
    script = {"flash-3.6": "429"}
    c = client(monkeypatch, script)
    chat(c, "flash-3.6", ["flash-3.5"])

    script.clear()                      # the quota window rolled over
    api.clear_cooldowns()               # stands in for the cooldown elapsing
    c.asked.clear()
    out = chat(c, "flash-3.6", ["flash-3.5"])
    assert c.asked[0] == "flash-3.6", "it never went back to the preferred model"
    assert out.model == "flash-3.6"


def test_everything_cooling_down_still_tries_rather_than_refusing(monkeypatch):
    """If the whole chain is resting, asking the preferred model and waiting
    beats refusing outright."""
    api.note_rate_limited("https://api.example/v1", "flash-3.6")
    api.note_rate_limited("https://api.example/v1", "flash-3.5")
    plan = api.plan_models("https://api.example/v1", "flash-3.6", ["flash-3.5"])
    assert plan == ["flash-3.6", "flash-3.5"]


def test_cooldowns_are_per_endpoint(monkeypatch):
    """The same model name on a different provider is a different quota."""
    api.note_rate_limited("https://a/v1", "m")
    assert api.is_cooling_down("https://a/v1", "m") is True
    assert api.is_cooling_down("https://b/v1", "m") is False


# --------------------------------------------------------------------- #
# Saying so

def test_the_result_records_which_model_answered(monkeypatch):
    c = client(monkeypatch, {"flash-3.6": "429"})
    assert chat(c, "flash-3.6", ["flash-3.5"]).model == "flash-3.5"


def test_the_agent_says_so_once_per_switch():
    """A silent switch is the worst version of this: the chat quietly gets
    worse at tool calling and nothing explains why. Said once, not per
    request -- it is news the first time and noise after that."""
    from glmcode.agent import Agent
    ag = Agent.__new__(Agent)
    said = []
    ag.events = types.SimpleNamespace(warn=said.append)

    ag._say_if_fallback("big", types.SimpleNamespace(model="small"))
    ag._say_if_fallback("big", types.SimpleNamespace(model="small"))
    assert len(said) == 1
    assert "rate-limited" in said[0] and "small" in said[0]

    # back on the preferred model: nothing to say, and the next switch is news
    ag._say_if_fallback("big", types.SimpleNamespace(model="big"))
    ag._say_if_fallback("big", types.SimpleNamespace(model="small"))
    assert len(said) == 2


def test_nothing_is_said_when_the_asked_for_model_answered():
    from glmcode.agent import Agent
    ag = Agent.__new__(Agent)
    said = []
    ag.events = types.SimpleNamespace(warn=said.append)
    ag._say_if_fallback("big", types.SimpleNamespace(model="big"))
    assert said == []


# --------------------------------------------------------------------- #
# The setting

def test_the_chain_is_cleaned_not_trusted():
    """It comes from a text field. A blank or duplicated entry would silently
    make the chain shorter than it looks."""
    import sys
    sys.modules.setdefault("webview", types.SimpleNamespace(
        Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(),
        SAVE_DIALOG=object()))
    from glmcode.config import Config
    from glmcode.gui import app as gui_app

    api_obj = gui_app.Api.__new__(gui_app.Api)
    api_obj._cfg = Config()
    api_obj._chats = {}
    api_obj.session_id = None
    gui_app.save_config = lambda c: None
    api_obj.set_setting("model_fallbacks", ["a", "", "  b  ", "a", None])
    assert api_obj._cfg.model_fallbacks == ["a", "b"]


def test_the_agent_passes_the_chain_through():
    from glmcode.agent import Agent
    from glmcode.config import Config
    ag = Agent.__new__(Agent)
    ag.cfg = Config()
    assert ag._fallback_models() == []
    ag.cfg.model_fallbacks = ["b", "", "c"]
    assert ag._fallback_models() == ["b", "c"]
