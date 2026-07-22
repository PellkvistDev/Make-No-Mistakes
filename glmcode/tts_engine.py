"""Thin dispatcher over the two local text-to-speech engines -- Kokoro (the
default, in tts.py) and Piper (piper.py). Everything that speaks (read-aloud,
voice-mode replies, the speak tool, the Settings preview) goes through here with
the configured engine, so the rest of the app never has to know which is active.

Both engines expose the same shape: synthesize() -> (float32 samples, sr),
list_voices(), ready(), save_wav(), prewarm(), DEFAULT_VOICE. Piper's per-voice
functions take a voice id; Kokoro's readiness is voice-independent."""

from __future__ import annotations

from pathlib import Path

from . import piper as _piper
from . import tts as _kokoro

ENGINES = ("kokoro", "piper")


def _mod(engine: str):
    return _piper if engine == "piper" else _kokoro


def default_voice(engine: str = "kokoro") -> str:
    return _mod(engine).DEFAULT_VOICE


def list_voices(engine: str = "kokoro") -> list[str]:
    return _mod(engine).list_voices()


def ready(engine: str = "kokoro", voice: str = "") -> bool:
    if engine == "piper":
        return _piper.ready(voice or _piper.DEFAULT_VOICE)
    return _kokoro.ready()


def synthesize(text: str, voice: str, speed: float = 1.0,
               engine: str = "kokoro", status=None):
    return _mod(engine).synthesize(text, voice=voice, speed=speed, status=status)


def save_wav(text: str, out_path: Path, voice: str, speed: float = 1.0,
             engine: str = "kokoro", status=None) -> Path:
    return _mod(engine).save_wav(text, out_path, voice=voice, speed=speed, status=status)


def prewarm(engine: str = "kokoro", voice: str = "") -> bool:
    if engine == "piper":
        return _piper.prewarm(voice or _piper.DEFAULT_VOICE)
    return _kokoro.prewarm()


def audio_to_wav_bytes(audio, sample_rate: int) -> bytes:
    return _kokoro.audio_to_wav_bytes(audio, sample_rate)  # shared encoder
