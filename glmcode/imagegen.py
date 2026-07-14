"""Local image generation with Stable Diffusion Turbo (stabilityai/sd-turbo).

Heavy ML dependencies (torch, diffusers, transformers, accelerate) are never
imported at module load time -- only inside functions, and only once actually
needed -- so importing this module costs nothing for users who never
generate an image. On first use, the packages are installed automatically
via pip; the model weights (~1.7GB) are then downloaded and cached by
huggingface_hub the first time the pipeline loads. Everything after that
runs fully offline.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

MODEL_ID = "stabilityai/sd-turbo"
REQUIRED_PACKAGES = ["torch", "diffusers", "transformers", "accelerate", "safetensors"]

StatusFn = Optional[Callable[[str], None]]

_pipe = None
# A local model pipeline is an expensive singleton (multi-GB in memory) and
# not meant for concurrent inference calls, so install/load/generate all
# serialize through this one lock -- concurrent callers (e.g. parallel
# sub-agents) queue rather than race.
_lock = threading.Lock()


def packages_installed() -> bool:
    """Cheap check (no heavy imports) so callers -- e.g. the permission
    prompt -- can tell whether the first-use install still needs to happen."""
    import importlib.util
    return all(
        importlib.util.find_spec(pkg) is not None
        for pkg in ("torch", "diffusers", "transformers", "accelerate")
    )


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status(
            "Installing local image-generation dependencies (first time only, "
            "~1-2GB download; this can take several minutes)..."
        )
    cmd = [sys.executable, "-m", "pip", "install", "--user", *REQUIRED_PACKAGES]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=900, **NO_WINDOW_KWARGS
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Installing image-generation dependencies timed out after 15 minutes."
        )
    except OSError as e:
        raise RuntimeError(f"Could not start pip: {e}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Failed to install image-generation dependencies:\n{tail}")


def _load_pipeline(status: StatusFn = None):
    global _pipe
    if _pipe is not None:
        return _pipe
    if not packages_installed():
        _install_packages(status)

    import torch
    from diffusers import AutoPipelineForText2Image

    if status:
        status(f"Loading {MODEL_ID} (downloading ~1.7GB the first time)...")
    use_cuda = torch.cuda.is_available()
    dtype = torch.float16 if use_cuda else torch.float32
    pipe = AutoPipelineForText2Image.from_pretrained(MODEL_ID, torch_dtype=dtype)
    pipe = pipe.to("cuda" if use_cuda else "cpu")
    if not use_cuda and status:
        status("No GPU detected; running on CPU (generation will be slower).")
    _pipe = pipe
    return _pipe


def generate_image(prompt: str, out_path: Path, steps: int = 1, status: StatusFn = None) -> Path:
    """Generate an image with sd-turbo and save it as a PNG at out_path."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt must not be empty")
    steps = max(1, min(int(steps or 1), 4))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        pipe = _load_pipeline(status)
        if status:
            status(f"Generating image ({steps} step{'s' if steps != 1 else ''})...")
        image = pipe(prompt=prompt, num_inference_steps=steps, guidance_scale=0.0).images[0]
        image.save(out_path)
    return out_path
