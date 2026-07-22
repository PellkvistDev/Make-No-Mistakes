"""The TTS engine selector: the dispatcher routes to Kokoro or Piper, each
keeps its own voice, and Piper's per-voice model bookkeeping is correct. The
actual audio synthesis (numpy/onnx) isn't exercised here -- only the wiring."""

import sys
import types

from glmcode import piper, tts_engine

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402


def test_app_picks_each_engines_own_voice():
    kokoro = types.SimpleNamespace(tts_engine="kokoro", tts_voice="af_sky", piper_voice="sv_SE-nst-medium")
    piper_cfg = types.SimpleNamespace(tts_engine="piper", tts_voice="af_sky", piper_voice="sv_SE-nst-medium")
    assert gui_app._tts_engine_voice(kokoro) == ("kokoro", "af_sky")
    assert gui_app._tts_engine_voice(piper_cfg) == ("piper", "sv_SE-nst-medium")
    # Missing/empty falls back sanely.
    assert gui_app._tts_engine_voice(types.SimpleNamespace()) == ("kokoro", "af_heart")


# -- dispatcher routing ---------------------------------------------------- #

def test_list_voices_and_default_per_engine():
    kv = tts_engine.list_voices("kokoro")
    pv = tts_engine.list_voices("piper")
    assert "af_heart" in kv                       # a Kokoro voice
    assert "en_US-amy-medium" in pv               # a Piper voice
    assert "sv_SE-nst-medium" in pv               # Swedish, the whole point
    assert tts_engine.default_voice("kokoro") == "af_heart"
    assert tts_engine.default_voice("piper") == "en_US-amy-medium"
    # Unknown engine falls back to Kokoro, never crashes.
    assert tts_engine.list_voices("bogus") == kv


def _fake_engines(monkeypatch):
    calls = {"k": [], "p": []}
    fake_k = types.SimpleNamespace(
        DEFAULT_VOICE="af_heart",
        list_voices=lambda: ["af_heart"],
        ready=lambda: True,
        prewarm=lambda: calls["k"].append("prewarm") or True,
        synthesize=lambda text, voice, speed, status=None: calls["k"].append(("syn", voice)),
        save_wav=lambda text, out, voice, speed, status=None: calls["k"].append(("save", voice)),
    )
    fake_p = types.SimpleNamespace(
        DEFAULT_VOICE="en_US-amy-medium",
        list_voices=lambda: ["en_US-amy-medium", "sv_SE-nst-medium"],
        ready=lambda v="en_US-amy-medium": True,
        prewarm=lambda v="en_US-amy-medium": calls["p"].append(("prewarm", v)) or True,
        synthesize=lambda text, voice, speed, status=None: calls["p"].append(("syn", voice)),
        save_wav=lambda text, out, voice, speed, status=None: calls["p"].append(("save", voice)),
    )
    monkeypatch.setattr(tts_engine, "_kokoro", fake_k)
    monkeypatch.setattr(tts_engine, "_piper", fake_p)
    return calls


def test_synthesize_routes_to_selected_engine(monkeypatch):
    calls = _fake_engines(monkeypatch)
    tts_engine.synthesize("hi", "af_heart", 1.0, engine="kokoro")
    tts_engine.synthesize("hej", "sv_SE-nst-medium", 1.0, engine="piper")
    assert calls["k"] == [("syn", "af_heart")]
    assert calls["p"] == [("syn", "sv_SE-nst-medium")]


def test_save_and_prewarm_and_ready_route(monkeypatch):
    calls = _fake_engines(monkeypatch)
    tts_engine.save_wav("hi", "/tmp/x.wav", "af_heart", 1.0, engine="kokoro")
    tts_engine.save_wav("hej", "/tmp/y.wav", "sv_SE-nst-medium", 1.0, engine="piper")
    assert ("save", "af_heart") in calls["k"]
    assert ("save", "sv_SE-nst-medium") in calls["p"]
    tts_engine.prewarm("piper", "sv_SE-nst-medium")
    assert ("prewarm", "sv_SE-nst-medium") in calls["p"]
    assert tts_engine.ready("kokoro") is True
    assert tts_engine.ready("piper", "sv_SE-nst-medium") is True


# -- piper model bookkeeping (no heavy deps) ------------------------------- #

def test_piper_voice_paths_and_download_state(monkeypatch, tmp_path):
    monkeypatch.setattr(piper, "MODEL_DIR", tmp_path)
    onnx, cfg = piper._voice_paths("sv_SE-nst-medium")
    assert onnx.name == "sv_SE-nst-medium.onnx"
    assert cfg.name == "sv_SE-nst-medium.onnx.json"
    assert piper.model_downloaded("sv_SE-nst-medium") is False   # nothing on disk yet
    onnx.parent.mkdir(parents=True, exist_ok=True)
    onnx.write_bytes(b"x")
    cfg.write_text("{}")
    assert piper.model_downloaded("sv_SE-nst-medium") is True


def test_piper_prewarm_and_ready_noop_without_package(monkeypatch):
    monkeypatch.setattr(piper, "packages_installed", lambda: False)
    assert piper.ready("en_US-amy-medium") is False
    assert piper.prewarm("en_US-amy-medium") is False   # never triggers install/download


def test_piper_raw_pcm_handles_stream_api():
    # Older piper-tts: synthesize_stream_raw(text, length_scale=...) -> byte iter.
    class OldVoice:
        def synthesize_stream_raw(self, text, length_scale=1.0):
            assert length_scale > 0
            yield b"\x01\x00\x02\x00"
    assert piper._raw_pcm(OldVoice(), "hi", 1.0) == b"\x01\x00\x02\x00"


def test_piper_raw_pcm_handles_chunk_api():
    # Newer piper-tts: synthesize(text) -> AudioChunk objects with int16 bytes.
    class Chunk:
        audio_int16_bytes = b"\x03\x00"
    class NewVoice:
        def synthesize(self, text):
            return [Chunk(), Chunk()]
    assert piper._raw_pcm(NewVoice(), "hi", 1.0) == b"\x03\x00\x03\x00"
