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
# diffusers (as of 0.39.0) only declares transformers>=4.41.2 as a test/dev
# extra -- its actual install has NO upper bound on transformers at all. An
# unpinned install can therefore grab a brand-new transformers major version
# diffusers was never built against, breaking at runtime (e.g. "cannot
# import name 'Dinov2WithRegistersConfig' from 'transformers'" when
# transformers 5.x lands alongside diffusers built for the 4.x line). Pin it
# ourselves since diffusers doesn't.
REQUIRED_PACKAGES = ["torch", "diffusers", "transformers<5", "accelerate", "safetensors"]

StatusFn = Optional[Callable[[str], None]]

_pipe = None
# A local model pipeline is an expensive singleton (multi-GB in memory) and
# not meant for concurrent inference calls, so install/load/generate all
# serialize through this one lock -- concurrent callers (e.g. parallel
# sub-agents) queue rather than race.
_lock = threading.Lock()


def packages_installed() -> bool:
    """Cheap check (no heavy imports) so callers -- e.g. the permission
    prompt -- can tell whether the first-use install still needs to happen.
    Also catches an already-installed-but-incompatible transformers (see
    REQUIRED_PACKAGES) so a broken install self-heals on the next call
    instead of silently staying broken forever."""
    import importlib.util
    if not all(
        importlib.util.find_spec(pkg) is not None
        for pkg in ("torch", "diffusers", "transformers", "accelerate")
    ):
        return False
    return _transformers_version_ok()


def _transformers_version_ok() -> bool:
    try:
        import importlib.metadata
        major = int(importlib.metadata.version("transformers").split(".")[0])
        return major < 5
    except Exception:
        return True  # can't tell -- don't block on an unrelated parsing issue


def _install_packages(status: StatusFn = None) -> None:
    from .tools import NO_WINDOW_KWARGS
    if status:
        status(
            "Installing local image-generation dependencies (first time only, "
            "~1-2GB download; this can take several minutes)..."
        )
    # If an incompatible transformers is already installed somewhere on
    # sys.path (e.g. in the interpreter's own site-packages), installing the
    # pinned version with --user can land in a *different* site-packages
    # directory that doesn't take precedence -- the broken copy keeps
    # shadowing the fix and the same import error comes back. Uninstall it
    # first so there is only ever one copy, wherever pip puts the new one.
    try:
        import importlib.util
        if importlib.util.find_spec("transformers") is not None and not _transformers_version_ok():
            subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", "transformers"],
                capture_output=True, text=True, timeout=120, **NO_WINDOW_KWARGS,
            )
    except Exception:
        pass
    # --upgrade matters here, not just for freshness: it's what actually
    # corrects an already-installed-but-incompatible transformers (see
    # REQUIRED_PACKAGES) -- without it, pip could see transformers is
    # "already installed" and leave the broken version in place.
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", *REQUIRED_PACKAGES]
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


def _purge_stale_modules() -> None:
    """Reinstalling transformers/diffusers on disk does nothing for a copy
    already imported into this same long-running process -- Python's import
    system returns the cached sys.modules entry instead of re-reading the
    fresh files, so a fixed install can still look broken until the app is
    restarted. Drop any cached copies so the next import actually reads
    what's on disk now."""
    for name in list(sys.modules):
        if name == "transformers" or name.startswith("transformers.") \
                or name == "diffusers" or name.startswith("diffusers."):
            del sys.modules[name]


def _load_pipeline(status: StatusFn = None):
    global _pipe
    if _pipe is not None:
        return _pipe
    if not packages_installed():
        _install_packages(status)
    # Unconditional, not just after a fresh install: if an earlier attempt
    # in this same process already got as far as `import transformers`
    # succeeding (it's diffusers' *deeper* submodule import that actually
    # fails), that now-stale copy is sitting in sys.modules regardless of
    # what packages_installed() reports this time -- a retry within the
    # same session would otherwise keep hitting the cached bad copy even
    # though the files on disk are already fixed.
    _purge_stale_modules()

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
