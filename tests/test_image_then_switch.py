"""The reported sequence: read an image on one model, then switch model.

    "i used gemini, told it to look at an image, then switched to glm, and
     then it automatically switched back to gemini, cuz of 'image in context'"

Three separate mechanisms conspired, and each is checked here:

  * _model_for_turn re-routed the whole turn whenever an image was ANYWHERE in
    the history, and the history is never re-examined, so it never wore off.
  * the destination was cfg.vision_model, which belonged to the provider chosen
    at setup -- so "somewhere else" was always the same somewhere else.
  * the GUI pinned agent.vision_client at that provider the moment a chat moved
    to another one, so even the image call itself ignored the chat's own API.
"""

import sys
import types

import glmcode.config as config_mod
from glmcode.config import Config

from conftest import FakeResult

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"
ZAI = "https://api.z.ai/api/paas/v4"
IMG = {"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
    {"type": "text", "text": "(user attached: shot.png)"}]}


def _google_install(monkeypatch, scripted_agent):
    """A config set up on Google, with z.ai added as a second API."""
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    cfg = Config(provider_preset="google", base_url=GOOGLE,
                 model="gemini-3.6-flash",
                 available_models=["gemini-3.6-flash", "gemini-3.5-flash-lite"])
    cfg.providers.append({"name": "Z.AI", "base_url": ZAI,
                          "api_key": "z-key", "models": ["glm-4.7-flash"]})
    agent = scripted_agent()
    agent.cfg = cfg
    agent.client.base_url = GOOGLE
    agent.model_override = "gemini-3.6-flash"
    return cfg, agent


def _api(cfg):
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = cfg
    api._chats = {}
    api._client = None
    api.session_id = None
    return api


def test_the_turn_stays_on_the_model_you_switched_to(monkeypatch, scripted_agent):
    cfg, agent = _google_install(monkeypatch, scripted_agent)
    agent.messages.append(IMG)
    assert agent._model_for_turn() == "gemini-3.6-flash"

    api = _api(cfg)
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    api._apply_chat_model(agent, "Z.AI", "glm-4.7-flash")

    # Every turn from here, not just the next one: the old check re-read the
    # whole history each time and kept finding the same image.
    for _ in range(3):
        assert agent._model_for_turn() == "glm-4.7-flash"
        agent.messages.append({"role": "user", "content": "and now?"})


def test_switching_does_not_leave_vision_aimed_at_the_old_api(monkeypatch,
                                                              scripted_agent):
    cfg, agent = _google_install(monkeypatch, scripted_agent)
    api = _api(cfg)
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    api._apply_chat_model(agent, "Z.AI", "glm-4.7-flash")

    client, model = agent._vision_client_and_model()
    assert model == "glm-4.6v-flash", "z.ai's own image model, not Gemini"
    assert client.base_url == ZAI


def test_a_model_that_cannot_see_is_not_sent_the_picture(monkeypatch,
                                                         scripted_agent):
    """The history still holds the image Gemini was given. Sent on to a model
    that does not implement image parts, the endpoint rejects the whole
    request -- which would make switching model a one-way door."""
    cfg, agent = _google_install(monkeypatch, scripted_agent)
    agent.messages.append(IMG)
    api = _api(cfg)
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)
    api._apply_chat_model(agent, "Z.AI", "glm-4.7-flash")

    sent = agent._messages_for_call()
    assert not any(
        isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
        for m in sent)
    # Replaced, not dropped: the file is still there to be read on request,
    # and its name is still in the message next to it.
    flat = str(sent)
    assert "view_image" in flat and "shot.png" in flat


def test_the_stored_history_keeps_the_image(monkeypatch, scripted_agent):
    """Only what goes on the wire is trimmed. The picture has to come back if
    the chat moves to a model that can see it again -- and this history is also
    what gets saved and synced to the phone."""
    cfg, agent = _google_install(monkeypatch, scripted_agent)
    agent.messages.append(IMG)
    api = _api(cfg)
    monkeypatch.setattr(gui_app, "save_config", lambda c: None)

    api._apply_chat_model(agent, "Z.AI", "glm-4.7-flash")
    agent._messages_for_call()
    api._apply_chat_model(agent, "Google AI Studio", "gemini-3.6-flash")

    assert any(p.get("type") == "image_url"
               for m in agent.messages if isinstance(m.get("content"), list)
               for p in m["content"])
    assert any(
        isinstance(m.get("content"), list)
        and any(p.get("type") == "image_url" for p in m["content"])
        for m in agent._messages_for_call())
