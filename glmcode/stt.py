"""Local speech-to-text with faster-whisper (CTranslate2).

Heavy deps (faster_whisper, which pulls ctranslate2) are never imported at
module load time -- only inside functions -- so importing this module costs
nothing until STT is actually used. First use installs faster-whisper via pip
and downloads the chosen Whisper model (cached under ~/.makenomistakes/models/
whisper/); every call after that runs fully offline. Runs real-time on CPU
with the small/base models (no GPU required); INT8 quantization keeps it fast.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from .config import CONFIG_DIR

MODEL_DIR = CONFIG_DIR / "models" / "whisper"
REQUIRED_PACKAGES = ["faster-whisper"]

# Whisper sizes, smallest/fastest first. "base" is the default: real-time on a
# plain CPU with solid accuracy for prompt dictation. English-only ".en"
# variants are a touch faster/better for English but can't do other languages.
MODELS = ["tiny", "base", "small", "medium",
          "tiny.en", "base.en", "small.en", "distil-small.en"]
DEFAULT_MODEL = "base"

StatusFn = Optional[Callable[[str], None]]

# One loaded model per size (WhisperModel is an expensive singleton and not
# meant for concurrent inference), and a lock so install/download/load/
# transcribe all serialize instead of racing.
_models: dict = {}
_lock = threading.Lock()


def packages_installed() -> bool:
    """Cheap check (no heavy imports) so callers can tell whether the
    first-use install still needs to happen."""
    import importlib.util
    return importlib.util.find_spec("faster_whisper") is not None


def model_downloaded(model: str = DEFAULT_MODEL) -> bool:
    """faster-whisper caches HF snapshots under MODEL_DIR; a non-empty dir for
    this model means it's already been fetched."""
    d = MODEL_DIR / ("models--" + _hf_repo(model).replace("/", "--"))
    try:
        return d.is_dir() and any(d.rglob("model.bin"))
    except OSError:
        return False


def ready(model: str = DEFAULT_MODEL) -> bool:
    return packages_installed() and model_downloaded(model)


def _hf_repo(model: str) -> str:
    # faster-whisper resolves bare sizes to the Systran CT2 repos.
    if "/" in model:
        return model
    if model.startswith("distil"):
        return f"distil-whisper/{model}"
    return f"Systran/faster-whisper-{model}"


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status("Installing local speech-to-text (faster-whisper, first time "
               "only, ~50MB)...")
    cmd = [sys.executable, "-m", "pip", "install", "--user", *REQUIRED_PACKAGES]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                              **NO_WINDOW_KWARGS)
    except subprocess.TimeoutExpired:
        raise RuntimeError("Installing speech-to-text timed out after 10 minutes.")
    except OSError as e:
        raise RuntimeError(f"Could not start pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to install speech-to-text:\n{tail}")


def _load_model(model: str, status: StatusFn = None):
    m = _models.get(model)
    if m is not None:
        return m
    if not packages_installed():
        _install_packages(status)
    if status and not model_downloaded(model):
        status(f"Downloading the '{model}' speech model (first time only)...")
    from faster_whisper import WhisperModel
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # int8 on CPU: ~2x faster than float, negligible accuracy loss.
    m = WhisperModel(model, device="cpu", compute_type="int8",
                     download_root=str(MODEL_DIR))
    _models[model] = m
    return m


def prewarm(model: str = DEFAULT_MODEL) -> bool:
    """Load the model into the cache ahead of the first transcription, so voice
    mode's first utterance isn't stuck behind a cold model load. Only warms if
    everything is already installed/downloaded -- never triggers the (large)
    first-use install/download as a surprise side effect. Returns True if a
    model is now resident. Safe to call from a background thread."""
    if model not in MODELS and "/" not in model:
        model = DEFAULT_MODEL
    if not ready(model):
        return False
    try:
        with _lock:
            _load_model(model)
        return True
    except Exception:
        return False


def transcribe(audio, model: str = DEFAULT_MODEL, language: str = "",
               status: StatusFn = None) -> str:
    """Transcribe speech to text. `audio` is a file path (any format
    faster-whisper's decoder handles: wav/webm/ogg/mp3/...) or a float32 numpy
    array of 16kHz mono samples. `language` '' auto-detects. Returns the
    transcript (empty string if only silence)."""
    if model not in MODELS and "/" not in model:
        model = DEFAULT_MODEL
    if isinstance(audio, Path):
        audio = str(audio)
    with _lock:
        m = _load_model(model, status)
        if status:
            status("Transcribing…")
        segments, _info = m.transcribe(
            audio,
            language=(language or None),
            vad_filter=True,          # Silero VAD: drop silence/noise gaps
            beam_size=1,              # greedy: fastest, plenty for dictation
        )
        text = "".join(seg.text for seg in segments)
    return text.strip()
