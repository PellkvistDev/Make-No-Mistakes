"""Local text-to-speech via Piper (https://github.com/rhasspy/piper) -- an
alternative engine to Kokoro. Piper has many natural voices across dozens of
languages (Swedish included), which is why it's offered alongside Kokoro.

Same lazy pattern as the other local models: the first use of a voice installs
the `piper-tts` package and downloads that one voice's model (~20-60MB), then
runs fully offline. synthesize() returns (float32 mono samples, sample_rate),
matching Kokoro's output so both flow through the same wav/playback path."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import CONFIG_DIR

StatusFn = Optional[Callable[[str], None]]

MODEL_DIR = CONFIG_DIR / "models" / "piper"
REQUIRED_PACKAGES = ["piper-tts"]
_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# A curated set of voices (id -> the voice's path within rhasspy/piper-voices).
# Each voice is two files: "<path>.onnx" and "<path>.onnx.json".
VOICES = {
    "en_US-amy-medium": "en/en_US/amy/medium/en_US-amy-medium",
    "en_US-lessac-medium": "en/en_US/lessac/medium/en_US-lessac-medium",
    "en_US-ryan-high": "en/en_US/ryan/high/en_US-ryan-high",
    "en_GB-alan-medium": "en/en_GB/alan/medium/en_GB-alan-medium",
    "sv_SE-nst-medium": "sv/sv_SE/nst/medium/sv_SE-nst-medium",
    "de_DE-thorsten-medium": "de/de_DE/thorsten/medium/de_DE-thorsten-medium",
    "es_ES-davefx-medium": "es/es_ES/davefx/medium/es_ES-davefx-medium",
    "fr_FR-siwis-medium": "fr/fr_FR/siwis/medium/fr_FR-siwis-medium",
    "it_IT-riccardo-x_low": "it/it_IT/riccardo/x_low/it_IT-riccardo-x_low",
    "nl_NL-mls-medium": "nl/nl_NL/mls/medium/nl_NL-mls-medium",
    "pt_BR-faber-medium": "pt/pt_BR/faber/medium/pt_BR-faber-medium",
    "ru_RU-dmitri-medium": "ru/ru_RU/dmitri/medium/ru_RU-dmitri-medium",
}
DEFAULT_VOICE = "en_US-amy-medium"

_voices: dict = {}   # voice id -> loaded PiperVoice
_lock = threading.Lock()


def list_voices() -> list[str]:
    return list(VOICES.keys())


def packages_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("piper") is not None


def _voice_paths(voice: str) -> tuple[Path, Path]:
    rel = VOICES.get(voice, VOICES[DEFAULT_VOICE])
    name = rel.rsplit("/", 1)[-1]
    d = MODEL_DIR / voice
    return d / f"{name}.onnx", d / f"{name}.onnx.json"


def model_downloaded(voice: str = DEFAULT_VOICE) -> bool:
    onnx, cfg = _voice_paths(voice)
    try:
        return onnx.is_file() and cfg.is_file()
    except OSError:
        return False


def ready(voice: str = DEFAULT_VOICE) -> bool:
    return packages_installed() and model_downloaded(voice)


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status("Installing Piper text-to-speech (first time only)...")
    cmd = [sys.executable, "-m", "pip", "install", "--user", *REQUIRED_PACKAGES]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                              **NO_WINDOW_KWARGS)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Installing Piper timed out after 10 minutes.")
    except OSError as e:
        raise RuntimeError(f"Could not start pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to install Piper:\n{tail}")


def _download_voice(voice: str, status: StatusFn = None) -> None:
    import requests
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    rel = VOICES[voice]
    onnx, cfg = _voice_paths(voice)
    onnx.parent.mkdir(parents=True, exist_ok=True)
    for url, path in ((_HF_BASE + rel + ".onnx", onnx),
                      (_HF_BASE + rel + ".onnx.json", cfg)):
        if path.is_file():
            continue
        if status:
            status(f"Downloading the '{voice}' voice (first time only)...")
        try:
            r = requests.get(url, timeout=120, stream=True)
            r.raise_for_status()
            tmp = path.with_suffix(path.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            tmp.replace(path)
        except Exception as e:
            raise RuntimeError(f"Could not download the '{voice}' Piper voice: {e}")


def _load_voice(voice: str, status: StatusFn = None):
    if voice not in VOICES:
        voice = DEFAULT_VOICE
    v = _voices.get(voice)
    if v is not None:
        return v
    if not packages_installed():
        _install_packages(status)
    if not model_downloaded(voice):
        _download_voice(voice, status)
    from piper import PiperVoice
    onnx, cfg = _voice_paths(voice)
    if status:
        status("Loading the Piper voice...")
    v = PiperVoice.load(str(onnx), config_path=str(cfg))
    _voices[voice] = v
    return v


def _sample_rate(v) -> int:
    try:
        return int(v.config.sample_rate)
    except Exception:
        return 22050


def _raw_pcm(v, text: str, length_scale: float) -> bytes:
    """Get raw 16-bit PCM bytes from a PiperVoice, tolerating API differences
    between piper-tts versions."""
    # Newer piper-tts: synthesize(text) yields AudioChunk objects.
    if hasattr(v, "synthesize"):
        try:
            out = bytearray()
            for chunk in v.synthesize(text):
                data = getattr(chunk, "audio_int16_bytes", None)
                if data is None and hasattr(chunk, "audio_float_array"):
                    import numpy as np
                    data = (np.clip(chunk.audio_float_array, -1, 1) * 32767).astype("int16").tobytes()
                if data:
                    out += data
            if out:
                return bytes(out)
        except TypeError:
            pass  # older signature -- fall through
    # Older piper-tts: synthesize_stream_raw(text, length_scale=...) -> bytes iter.
    if hasattr(v, "synthesize_stream_raw"):
        try:
            return b"".join(v.synthesize_stream_raw(text, length_scale=length_scale))
        except TypeError:
            return b"".join(v.synthesize_stream_raw(text))
    raise RuntimeError("This piper-tts version isn't supported.")


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0,
               status: StatusFn = None):
    """Return (float32 mono samples in [-1, 1], sample_rate) for `text`."""
    import numpy as np
    with _lock:
        v = _load_voice(voice, status)
    # Piper's length_scale is the inverse of speed (longer = slower).
    try:
        speed = float(speed) or 1.0
    except (TypeError, ValueError):
        speed = 1.0
    length_scale = 1.0 / max(0.5, min(2.0, speed))
    pcm = _raw_pcm(v, text, length_scale)
    audio = np.frombuffer(pcm, dtype=np.int16).astype("float32") / 32768.0
    return audio, _sample_rate(v)


def save_wav(text: str, out_path: Path, voice: str = DEFAULT_VOICE,
             speed: float = 1.0, status: StatusFn = None) -> Path:
    from .tts import audio_to_wav_bytes  # shared wav encoder
    audio, sr = synthesize(text, voice=voice, speed=speed, status=status)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio_to_wav_bytes(audio, sr))
    return out_path


def prewarm(voice: str = DEFAULT_VOICE) -> bool:
    """Load a voice ahead of first use, but only if already installed/downloaded
    (never triggers a surprise download). Safe on a background thread."""
    if not ready(voice):
        return False
    try:
        with _lock:
            _load_voice(voice)
        return True
    except Exception:
        return False
