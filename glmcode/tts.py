"""Local text-to-speech with Kokoro (via kokoro-onnx + onnxruntime).

Heavy deps (kokoro_onnx, onnxruntime, soundfile) are never imported at
module load time -- only inside functions -- so importing this module costs
nothing until TTS is actually used. First use installs those packages via
pip and downloads the two Kokoro model files (~300MB total, cached under
~/.makenomistakes/models/kokoro/); every call after that runs fully offline.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import CONFIG_DIR

MODEL_DIR = CONFIG_DIR / "models" / "kokoro"
MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
MODEL_PATH = MODEL_DIR / "kokoro-v1.0.onnx"
VOICES_PATH = MODEL_DIR / "voices-v1.0.bin"

REQUIRED_PACKAGES = ["kokoro-onnx", "soundfile"]
DEFAULT_VOICE = "af_heart"

StatusFn = Optional[Callable[[str], None]]

_kokoro = None
# A loaded ONNX session is an expensive singleton and not meant for
# concurrent inference calls, so install/download/load/synthesize all
# serialize through this one lock -- concurrent callers (e.g. the live
# read-aloud worker and a `speak` tool call at the same time) queue rather
# than race.
_lock = threading.Lock()


def packages_installed() -> bool:
    """Cheap check (no heavy imports) so callers -- e.g. the permission
    prompt or the read-aloud toggle -- can tell whether the first-use
    install/download still needs to happen."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None for m in ("kokoro_onnx", "soundfile"))


def model_downloaded() -> bool:
    return MODEL_PATH.is_file() and VOICES_PATH.is_file()


def ready() -> bool:
    """True once TTS can run with no further first-use setup."""
    return packages_installed() and model_downloaded()


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status(
            "Installing local text-to-speech dependencies (first time only, "
            "~50MB download)..."
        )
    cmd = [sys.executable, "-m", "pip", "install", "--user", *REQUIRED_PACKAGES]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, **NO_WINDOW_KWARGS
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Installing text-to-speech dependencies timed out after 10 minutes.")
    except OSError as e:
        raise RuntimeError(f"Could not start pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to install text-to-speech dependencies:\n{tail}")


def _download_models(status: StatusFn = None) -> None:
    import requests
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for url, path, label in ((VOICES_URL, VOICES_PATH, "voices"), (MODEL_URL, MODEL_PATH, "model")):
        if path.is_file():
            continue
        if status:
            status(f"Downloading Kokoro {label} file (first time only, ~300MB total)...")
        tmp = path.with_name(path.name + ".part")
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        if chunk:
                            f.write(chunk)
            tmp.replace(path)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to download Kokoro {label} file: {e}")


def _load_kokoro(status: StatusFn = None):
    global _kokoro
    if _kokoro is not None:
        return _kokoro
    if not packages_installed():
        _install_packages(status)
    if not model_downloaded():
        _download_models(status)
    from kokoro_onnx import Kokoro
    if status:
        status("Loading Kokoro text-to-speech model...")
    _kokoro = Kokoro(str(MODEL_PATH), str(VOICES_PATH))
    return _kokoro


def prewarm() -> bool:
    """Load the TTS model into memory ahead of the first spoken reply (so voice
    mode doesn't pay a cold-load stall on its first sentence). Only warms when
    already installed/downloaded -- never kicks off the first-use install as a
    surprise. Returns True if the model is resident. Safe on a bg thread."""
    if not ready():
        return False
    try:
        _load_kokoro()
        return True
    except Exception:
        return False


# The standard Kokoro-82M English voice set, for the UI to show BEFORE the
# model is downloaded (loading it just to list voices isn't worth a
# multi-hundred-MB fetch). Once loaded, list_voices() returns the real,
# authoritative list instead -- and synthesize() already falls back to
# DEFAULT_VOICE for any name that turns out not to exist, so this being
# slightly stale is never a hard failure, just a UI nicety.
FALLBACK_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]


def list_voices() -> list[str]:
    """Voices available once the model is loaded, or the standard fallback
    set if it hasn't been loaded yet this session."""
    if _kokoro is None:
        return list(FALLBACK_VOICES)
    return _kokoro.get_voices()


_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


def strip_code_fences_incremental(text: str) -> str:
    """Like clean_for_speech's fence stripping, but safe to call on a partial/
    streaming string: complete fenced blocks are removed, and a still-open
    trailing fence (still streaming inside a code block) is withheld
    entirely rather than spoken, since it isn't finished yet. Used by the
    live read-aloud buffer, which re-derives this from the full text seen so
    far on every delta rather than trying to parse fence markers that may be
    split across separate streaming chunks."""
    text = _CODE_FENCE_RE.sub(" ", text)
    if text.count("```") % 2 == 1:
        text = text[: text.rfind("```")]
    return text


def clean_for_speech(text: str) -> str:
    """Strip markdown/code artifacts a TTS engine shouldn't read literally
    (backticks, asterisks, headers, bullets, raw URLs, fenced code blocks)."""
    text = _CODE_FENCE_RE.sub(" ", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("`", "")  # catches lone/unpaired backticks (e.g. a
    # fence marker split awkwardly across two streaming deltas)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"(\*\*|__|\*|_)", "", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[ \t]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"https?://\S+", "link", text)
    return re.sub(r"\s+", " ", text).strip()


def synthesize(text: str, voice: str = DEFAULT_VOICE, speed: float = 1.0,
              status: StatusFn = None):
    """Synthesize speech. Returns (audio: np.ndarray float32, sample_rate: int)."""
    text = clean_for_speech(text)
    if not text:
        raise ValueError("nothing to say after removing code/markdown")
    speed = max(0.5, min(float(speed or 1.0), 2.0))
    with _lock:
        kokoro = _load_kokoro(status)
        voices = kokoro.get_voices()
        if voice not in voices:
            voice = DEFAULT_VOICE if DEFAULT_VOICE in voices else voices[0]
        if status:
            status("Synthesizing speech...")
        audio, sr = kokoro.create(text, voice=voice, speed=speed)
    return audio, sr


def audio_to_wav_bytes(audio, sample_rate: int) -> bytes:
    import io
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    return buf.getvalue()


def save_wav(text: str, out_path: Path, voice: str = DEFAULT_VOICE, speed: float = 1.0,
            status: StatusFn = None) -> Path:
    audio, sr = synthesize(text, voice=voice, speed=speed, status=status)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    sf.write(str(out_path), audio, sr)
    return out_path
