"""Configuration handling for GLM Code.

Config lives at ~/.glmcode/config.json. The API key can also come from the
ZAI_API_KEY environment variable (takes precedence over the config file).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_DIR = Path.home() / ".glmcode"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
# User-level memory: durable facts/preferences the agent has been asked to
# remember, loaded into the system prompt for every chat in every project
# (unlike GLM.md, which is per-project).
MEMORY_FILE = CONFIG_DIR / "memory.md"

DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"        # free coding model
DEFAULT_VISION_MODEL = "glm-4.6v-flash"  # free vision model

PERMISSION_MODES = ("ask", "autoedit", "yolo")


@dataclass
class Config:
    api_key: str = ""  # legacy only; the real source is the ZAI_API_KEY env var
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    mode: str = "ask"                # ask | autoedit | yolo
    temperature: float = 0.6
    max_tokens: int = 16384
    thinking: bool = True            # GLM reasoning mode
    show_reasoning: bool = True      # print the model's reasoning (dimmed)
    vision_route: str = "describe"   # describe | direct
    context_limit_tokens: int = 155_000  # hard auto-compact fallback above this estimate
    max_turns_per_request: int = 200  # safety cap on agentic iterations
    search_provider: str = "auto"    # auto | ddg | tavily
    tavily_api_key: str = ""         # optional upgrade: free key at tavily.com
    background_path: str = ""        # desktop app: custom background image
    gui_cwd: str = ""                # unused (kept for old config compatibility)
    last_session_id: str = ""        # desktop app: session to resume on launch
    read_aloud: bool = False         # desktop app: auto-speak assistant replies (Kokoro TTS)
    tts_voice: str = "af_heart"      # Kokoro voice name
    tts_speed: float = 1.0           # Kokoro speech speed, 0.5-2.0

    extra: dict = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        return os.environ.get("ZAI_API_KEY", "").strip() or self.api_key

    def resolve_tavily_key(self) -> str:
        return os.environ.get("TAVILY_API_KEY", "").strip() or self.tavily_api_key


def load_config() -> Config:
    cfg = Config()
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k) and k != "extra":
                    setattr(cfg, k, v)
                else:
                    cfg.extra[k] = v
        except (json.JSONDecodeError, OSError):
            pass
    if cfg.mode not in PERMISSION_MODES:
        cfg.mode = "ask"
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    extra = data.pop("extra", {})
    data.update(extra)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
