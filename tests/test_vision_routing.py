"""Vision routing: describe (default) vs direct, across both entry points
(user uploads and the model's own view_image), and the model-selection rules
that keep a custom multimodal model on itself while the free model falls back
to GLM vision."""

from pathlib import Path

import pytest

from conftest import FakeResult, tool_call


def _tiny_png(tmp_path) -> Path:
    # a 1x1 PNG -- small enough to embed without tripping any size cap
    import base64
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    p = tmp_path / "shot.png"
    p.write_bytes(data)
    return p


# -- model selection ------------------------------------------------------ #

def test_no_images_uses_chat_model(scripted_agent):
    agent = scripted_agent()
    agent.model_override = "custom/coder"
    assert agent._model_for_turn() == "custom/coder"


def test_images_describe_mode_routes_to_vision_model(scripted_agent):
    agent = scripted_agent()
    agent.model_override = "custom/coder"
    agent.cfg.vision_route = "describe"
    agent.messages.append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]})
    assert agent._model_for_turn() == agent.cfg.vision_model


def test_images_direct_mode_keeps_custom_model(scripted_agent):
    agent = scripted_agent()
    agent.model_override = "custom/multimodal"
    agent.cfg.vision_route = "direct"
    agent.messages.append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]})
    # the user's multimodal model sees the image itself
    assert agent._model_for_turn() == "custom/multimodal"


def test_images_direct_mode_builtin_falls_back_to_vision(scripted_agent):
    agent = scripted_agent()
    agent.model_override = None  # the free built-in model (coding model, blind)
    agent.cfg.vision_route = "direct"
    agent.messages.append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]})
    assert agent._model_for_turn() == agent.cfg.vision_model


# -- uploads -------------------------------------------------------------- #

def test_relative_image_path_and_uploads_resolve_against_workdir(scripted_agent, tmp_path):
    """Regression: _resolve_existing_image and _display_path were @staticmethods
    that referenced self.workdir, so every relative view_image path and every
    file upload raised NameError. Both must resolve against the chat's workdir."""
    agent = scripted_agent()
    agent.workdir = tmp_path
    img = _tiny_png(tmp_path)
    # relative path resolves under workdir (no NameError)
    assert agent._resolve_existing_image("shot.png", "view_image") == img
    # upload reference uses a workdir-relative display path
    (tmp_path / "doc.txt").write_text("x", encoding="utf-8")
    msg = agent.attach_files("hi", [tmp_path / "doc.txt"])
    assert "uploads" in msg["content"] and "doc.txt" in msg["content"]


def test_upload_describe_mode_uses_path_reference(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "describe"
    img = _tiny_png(tmp_path)
    msg = agent.attach_uploads("look", [img])
    # text content with an uploads/ path reference -- NOT embedded image parts
    assert isinstance(msg["content"], str)
    assert "uploads" in msg["content"] and "shot.png" in msg["content"]


def test_upload_direct_mode_embeds_image(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "direct"
    img = _tiny_png(tmp_path)
    msg = agent.attach_uploads("what is this", [img])
    parts = msg["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)          # embedded
    assert any(p.get("type") == "text" and "what is this" in p["text"] for p in parts)


def test_direct_mode_embeds_at_mentioned_image(scripted_agent, tmp_path):
    """An @-mentioned image (not a composer upload) still gets embedded in
    direct mode via embed_images, so the model sees it."""
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "direct"
    img = _tiny_png(tmp_path)
    msg = agent.attach_uploads("what is generated/shot.png", [], embed_images=[img])
    parts = msg["content"]
    assert isinstance(parts, list)
    assert any(p.get("type") == "image_url" for p in parts)


def test_describe_mode_leaves_mentioned_image_as_path(scripted_agent, tmp_path):
    """In describe mode an @-mentioned image is NOT embedded -- its clean path
    stays in the text for the model to view_image (GLM vision)."""
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "describe"
    img = _tiny_png(tmp_path)
    msg = agent.attach_uploads("what is shot.png", [], embed_images=[img])
    assert isinstance(msg["content"], str)  # plain text, no embedded image
    assert "shot.png" in msg["content"]


def test_upload_direct_mode_mixes_image_and_other_file(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "direct"
    img = _tiny_png(tmp_path)
    doc = tmp_path / "notes.txt"
    doc.write_text("hello", encoding="utf-8")
    msg = agent.attach_uploads("", [img, doc])
    parts = msg["content"]
    assert any(p.get("type") == "image_url" for p in parts)          # image embedded
    text = next(p["text"] for p in parts if p.get("type") == "text")
    assert "notes.txt" in text and "uploads" in text                 # doc referenced


# -- view_image ----------------------------------------------------------- #

def test_view_image_describe_mode_calls_vision_model(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "describe"
    img = _tiny_png(tmp_path)
    called = {}

    class FakeVision:
        def analyze_images(self, model, prompt, paths):
            called["model"] = model
            return "a red dot"

    agent.client = FakeVision()
    out = agent._view_image(str(img))
    assert out == "a red dot"
    assert called["model"] == agent.cfg.vision_model
    assert agent._pending_images == []  # nothing queued in describe mode


def test_view_image_direct_mode_queues_image_no_vision_call(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent.workdir = tmp_path
    agent.cfg.vision_route = "direct"
    img = _tiny_png(tmp_path)

    class BoomVision:
        def analyze_images(self, *a, **k):
            raise AssertionError("vision model must NOT be called in direct mode")

    agent.client = BoomVision()
    out = agent._view_image(str(img))
    assert "Attached" in out and "shot.png" in out
    assert len(agent._pending_images) == 1
    assert agent._pending_images[0][0] == "shot.png"


def test_inject_pending_images_appends_user_message(scripted_agent, tmp_path):
    agent = scripted_agent()
    agent._pending_images = [("a.png", "data:image/png;base64,AAAA")]
    agent._inject_pending_images()
    last = agent.messages[-1]
    assert last["role"] == "user"
    assert any(p.get("type") == "image_url" for p in last["content"])
    assert agent._pending_images == []  # flushed


def test_direct_view_image_end_to_end_stays_on_multimodal_model(scripted_agent, tmp_path):
    """The whole flow: a custom multimodal model calls view_image in direct
    mode; the image is injected into the conversation and the SAME model
    (not GLM vision) sees it on the next step."""
    img = _tiny_png(tmp_path)
    models_seen = []
    calls = iter([
        FakeResult(tool_calls=[tool_call("v1", "view_image",
                                         '{"path": "%s"}' % img.name)]),
        FakeResult(content="It's a tiny red dot."),
    ])

    agent = scripted_agent(lambda n: next(calls))
    agent.set_mode("yolo")
    agent.workdir = tmp_path
    agent.model_override = "custom/multimodal"
    agent.cfg.vision_route = "direct"

    orig_chat = agent.client.chat

    def spy(**kwargs):
        models_seen.append(kwargs.get("model"))
        return orig_chat(**kwargs)

    agent.client.chat = spy
    agent.run_turn({"role": "user", "content": "what's in shot.png"})

    # an image was injected as a user message (direct mode, no GLM-vision call)
    assert any(isinstance(m.get("content"), list)
               and any(p.get("type") == "image_url" for p in m["content"])
               for m in agent.messages)
    # every model call used the custom multimodal model -- never GLM vision
    assert models_seen and all(m == "custom/multimodal" for m in models_seen)
    assert agent.cfg.vision_model not in models_seen
