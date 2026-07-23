"""Local speech-to-text (faster-whisper wrapper) + the transcribe_audio bridge
that turns a recorded clip from the mic button into composer text. The heavy
model is mocked, so these run with no faster-whisper install and no audio."""

import base64
import sys
import types

import pytest

from glmcode import stt

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402


class _Seg:
    def __init__(self, text):
        self.text = text


class FakeWhisper:
    """Stand-in for faster_whisper.WhisperModel: records how it was called and
    returns scripted segments."""
    last_kwargs = None

    def __init__(self, model, **kw):
        self.model = model
        FakeWhisper.init_kwargs = kw

    def transcribe(self, audio, **kw):
        FakeWhisper.last_audio = audio
        FakeWhisper.last_kwargs = kw
        return ([_Seg(" Hello "), _Seg("world.")], {"language": "en"})


@pytest.fixture(autouse=True)
def _clean():
    stt._models.clear()
    yield
    stt._models.clear()


def _patch_model(monkeypatch, fake=FakeWhisper):
    monkeypatch.setattr(stt, "packages_installed", lambda: True)
    monkeypatch.setattr(stt, "model_downloaded", lambda m=stt.DEFAULT_MODEL: True)
    fake_mod = types.SimpleNamespace(WhisperModel=fake)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)


def test_transcribe_returns_joined_stripped_text(monkeypatch):
    _patch_model(monkeypatch)
    out = stt.transcribe("/tmp/clip.webm", model="base")
    assert out == "Hello world."
    # VAD on + greedy decode (fast, plenty for dictation)
    assert FakeWhisper.last_kwargs["vad_filter"] is True
    assert FakeWhisper.last_kwargs["beam_size"] == 1
    assert FakeWhisper.init_kwargs["compute_type"] == "int8"


def test_language_empty_becomes_auto(monkeypatch):
    _patch_model(monkeypatch)
    stt.transcribe("/tmp/clip.webm", language="")
    assert FakeWhisper.last_kwargs["language"] is None
    stt.transcribe("/tmp/clip.webm", language="sv")
    assert FakeWhisper.last_kwargs["language"] == "sv"


def test_unknown_model_falls_back_to_default(monkeypatch):
    seen = {}
    class Rec(FakeWhisper):
        def __init__(self, model, **kw):
            seen["model"] = model
            super().__init__(model, **kw)
    _patch_model(monkeypatch, Rec)
    stt.transcribe("/tmp/clip.webm", model="not-a-real-size")
    assert seen["model"] == stt.DEFAULT_MODEL


def test_model_is_cached_between_calls(monkeypatch):
    calls = {"n": 0}
    class Counting(FakeWhisper):
        def __init__(self, model, **kw):
            calls["n"] += 1
            super().__init__(model, **kw)
    _patch_model(monkeypatch, Counting)
    stt.transcribe("/tmp/a.webm", model="base")
    stt.transcribe("/tmp/b.webm", model="base")
    assert calls["n"] == 1   # loaded once, reused


def test_prewarm_noop_when_not_ready(monkeypatch):
    # Never trigger the (large) first-use install/download as a side effect of
    # pre-warming -- only warm when everything is already present.
    monkeypatch.setattr(stt, "ready", lambda m=stt.DEFAULT_MODEL: False)
    loaded = {"n": 0}
    monkeypatch.setattr(stt, "_load_model", lambda *a, **k: loaded.__setitem__("n", loaded["n"] + 1))
    assert stt.prewarm("base") is False
    assert loaded["n"] == 0


def test_prewarm_loads_when_ready(monkeypatch):
    monkeypatch.setattr(stt, "ready", lambda m=stt.DEFAULT_MODEL: True)
    loaded = {"n": 0}
    monkeypatch.setattr(stt, "_load_model", lambda *a, **k: loaded.__setitem__("n", loaded["n"] + 1))
    assert stt.prewarm("base") is True
    assert loaded["n"] == 1


def test_hf_repo_resolution():
    assert stt._hf_repo("base") == "Systran/faster-whisper-base"
    assert stt._hf_repo("distil-small.en") == "distil-whisper/distil-small.en"
    assert stt._hf_repo("org/custom") == "org/custom"


# -- the transcribe_audio bridge --------------------------------------- #

def _api(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_app, "CONFIG_DIR", tmp_path)
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = types.SimpleNamespace(stt_model="base", stt_language="")
    api._events_global = types.SimpleNamespace(info=lambda *a, **k: None)
    api._chats, api.session_id = {}, None
    return api


_WEBM = "data:audio/webm;base64," + base64.b64encode(b"x" * 2048).decode()


def test_transcribing_status_is_not_saved_to_chat(monkeypatch, tmp_path):
    # The routine "Transcribing…" must NOT become a saved chat notice; only the
    # one-time install/download should surface, and only as a transient toast.
    api = _api(monkeypatch, tmp_path)
    events = types.SimpleNamespace(
        info=lambda *a, **k: events.notices.append(a[0] if a else ""),
        toast=lambda msg, level="info": events.toasts.append(msg),
    )
    events.notices, events.toasts = [], []
    api._events_global = events

    def fake_transcribe(path, model="", language="", status=None):
        status("Transcribing…")                    # per-clip, should be dropped
        status("Downloading the 'base' speech model (first time only)...")  # one-time -> toast
        return "hello"

    monkeypatch.setattr("glmcode.stt.transcribe", fake_transcribe)
    assert api.transcribe_audio(_WEBM) == {"text": "hello"}
    assert events.notices == []                    # nothing saved into the chat
    assert any("Downloading" in t for t in events.toasts)
    assert not any("Transcrib" in t for t in events.toasts)


def test_transcribe_audio_returns_text_and_cleans_up(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    seen = {}
    def fake_transcribe(path, model="", language="", status=None):
        seen["path"] = str(path)
        seen["existed"] = __import__("pathlib").Path(path).is_file()
        return "transcribed text"
    monkeypatch.setattr("glmcode.stt.transcribe", fake_transcribe)

    res = api.transcribe_audio(_WEBM)
    assert res == {"text": "transcribed text"}
    assert seen["existed"] is True                    # the temp clip was written...
    assert not __import__("pathlib").Path(seen["path"]).exists()  # ...and cleaned up


def test_transcribe_audio_rejects_non_audio(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    assert "error" in api.transcribe_audio("data:image/png;base64,AAAA")
    assert "error" in api.transcribe_audio("not a data url")


def test_transcribe_audio_treats_tiny_clip_as_silence(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    tiny = "data:audio/webm;base64," + base64.b64encode(b"x").decode()
    assert api.transcribe_audio(tiny) == {"text": ""}


def test_transcribe_audio_surfaces_engine_errors(monkeypatch, tmp_path):
    api = _api(monkeypatch, tmp_path)
    monkeypatch.setattr("glmcode.stt.transcribe",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model boom")))
    res = api.transcribe_audio(_WEBM)
    assert "error" in res and "model boom" in res["error"]
