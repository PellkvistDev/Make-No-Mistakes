"""Configuration handling for GLM Code.

Config lives at ~/.makenomistakes/config.json. The API key can also come from
the ZAI_API_KEY environment variable (takes precedence over the config file).
"""

from __future__ import annotations

import json
import os
import shutil
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

_LEGACY_CONFIG_DIR = Path.home() / ".glmcode"
CONFIG_DIR = Path.home() / ".makenomistakes"

# Deliberately OUTSIDE CONFIG_DIR: if the failure is "can't create/write
# CONFIG_DIR at all", a note written inside it would never be seen either.
# This is the only trace a migration failure leaves -- nothing else in the
# app is set up yet this early (logger.py itself imports config, so it
# isn't safe to use here without a circular import).
_MIGRATION_LOG = Path.home() / "makenomistakes-migration-error.log"


def _log_migration_failure(exc: Exception) -> None:
    try:
        with _MIGRATION_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"\n--- {datetime.now(timezone.utc).isoformat()} ---\n")
            fh.write("".join(traceback.format_exception(exc)))
    except OSError:
        pass


def migrate_legacy_dir(old: Path | None = None, new: Path | None = None) -> bool:
    """One-time move of the old ~/.glmcode data dir to ~/.makenomistakes.

    Runs at import time, before any module touches CONFIG_DIR (logger.py
    mkdirs it on import, and everything imports config first). Never
    clobbers an existing new dir, never follows a symlinked old one, and
    never raises -- worst case the app starts with a fresh dir (a failure
    is logged to _MIGRATION_LOG, since a silent one would mean losing
    sessions/backups/memory/API key/background with no trace at all).
    """
    old = old if old is not None else _LEGACY_CONFIG_DIR
    new = new if new is not None else CONFIG_DIR
    try:
        if new.exists() or old.is_symlink() or not old.is_dir():
            return False
        try:
            old.rename(new)
            return True
        except OSError:
            pass  # e.g. a file inside is locked by another process on Windows
        # Fall back to copying, staged in a temp sibling dir first: copytree
        # partway through a failure would otherwise leave `new` half
        # populated, and the NEXT launch's `new.exists()` check above would
        # then mistake that for "already migrated" and skip forever,
        # permanently orphaning whatever didn't get copied.
        staging = new.with_name(new.name + ".migrating")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.copytree(old, staging)
            staging.rename(new)
            return True
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            _log_migration_failure(e)
            return False
    except Exception as e:
        _log_migration_failure(e)
        return False


migrate_legacy_dir()
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

# The always-available built-in provider (the free default). Custom providers
# (any OpenAI-compatible endpoint: OpenRouter, Ollama, LM Studio, paid APIs)
# are stored in Config.providers with the same shape.
BUILTIN_PROVIDER_NAME = "z.ai (free)"


def builtin_provider(cfg: "Config") -> dict:
    return {"name": BUILTIN_PROVIDER_NAME, "base_url": cfg.base_url,
            "api_key": cfg.resolve_api_key(),
            "models": [cfg.model, cfg.vision_model], "builtin": True}


def all_providers(cfg: "Config") -> list:
    return [builtin_provider(cfg)] + list(cfg.providers)


def find_provider(cfg: "Config", name: str) -> dict | None:
    for p in all_providers(cfg):
        if p.get("name") == name:
            return p
    return None


@dataclass
class Config:
    api_key: str = ""  # legacy only; the real source is the ZAI_API_KEY env var
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    mode: str = "ask"                # ask | autoedit | yolo
    temperature: float = 0.6
    max_tokens: int = 16384
    thinking: bool = True            # GLM reasoning mode (derived from thinking_mode; kept for compat)
    thinking_mode: str = "medium"    # low | medium | high | max (effort/iteration level)
    verify_edits: bool = False       # nudge the agent to verify edits it never ran anything to check (off by default)
    auto_fix_tests: bool = False     # "make it green": after an edit turn, run the project's tests and fix until they pass (opt-in, bounded)
    codebase_memory_neural: bool = False  # search_code uses a local embedding model (semantic) instead of lexical TF-IDF
    parallel_attempts: int = 1       # "race": 1 = off; 2 or 3 = run that many isolated attempts from a common baseline and keep the best
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
    notifications: bool = True       # desktop app: OS toasts while the window is unfocused
    reduce_effects: bool = True      # desktop app: blur-free fast theme (default); glass is opt-in
    browser_headless: bool = False   # control_chrome: hide the browser window (default: watch it)
    browser_keep_logins: bool = False  # control_chrome: persistent agent profile (logins survive restarts)
    browser_provider: str = ""       # control_chrome: dedicated Browser Agent provider ("" = same as chat)
    browser_model: str = ""          # control_chrome: dedicated Browser Agent model ("" = same as chat)
    tts_engine: str = "kokoro"       # text-to-speech engine: "kokoro" or "piper"
    tts_voice: str = "af_heart"      # Kokoro voice name
    piper_voice: str = "en_US-amy-medium"  # Piper voice id (used when tts_engine == "piper")
    stt_model: str = "base"          # faster-whisper model size for voice input
    stt_language: str = ""           # "" = auto-detect language
    tts_speed: float = 1.0           # Kokoro speech speed, 0.5-2.0
    voice_sensitivity: float = 1.0   # mic sensitivity for voice mode, 0.5-2.0 (higher = picks up quieter speech)
    voice_earcons: bool = True       # short tones on turn hand-off in voice mode
    voice_ptt_key: str = "Space"     # push-to-talk key (KeyboardEvent.code)
    voice_silence_ms: int = 750      # trailing silence (ms) that ends your turn, 400-1600
    voice_wake_enabled: bool = False # listen for a wake word to start voice mode hands-free
    voice_wake_word: str = "hey assistant"  # the spoken phrase that starts a voice session
    voice_wake_gated: bool = True    # require the wake word before EACH request (soft-mute between)
    voice_reply_language: str = "en"  # spoken reply language: "en" or "match" (the user's spoken language)
    # Custom model providers: [{"name", "base_url", "api_key", "models": [..]}].
    # Any OpenAI-compatible endpoint works; chats pick a provider+model in
    # Settings (per chat -- the free z.ai default stays the default).
    providers: list = field(default_factory=list)
    # MCP servers: [{"name", "command"}] -- command is a full shell command
    # line for a stdio MCP server (e.g. "npx -y @modelcontextprotocol/
    # server-filesystem C:\\projects"). Managed in Settings -> MCP servers.
    mcp_servers: list = field(default_factory=list)
    # Custom slash commands: [{"name", "template"}] reusable prompts invoked
    # with /name in the composer. $INPUT in the template is replaced by any
    # text typed after the command (else appended).
    commands: list = field(default_factory=list)
    # Scheduled & watched tasks: saved prompts that run themselves on an
    # interval / at a daily time / when a folder changes (see scheduler.py).
    # Each: {id, name, prompt, cwd, schedule, enabled, last_run, last_sig}.
    scheduled_tasks: list = field(default_factory=list)
    # Scoped autonomy: per-path permission rules [{"glob", "action"}] where
    # action is allow | ask | deny. They override the permission mode for file
    # writes (see permissions.path_rule_action): trusted paths auto-approve even
    # in "ask" mode, protected paths prompt/block even in "yolo".
    path_rules: list = field(default_factory=list)
    # GitHub integration: where cloned repos land ("" = the default sibling of
    # the app + whiteboard folders, resolved in the GUI), and whether a
    # connected session auto-pulls on open / auto-pushes after a change. The
    # token itself is NEVER stored here -- it lives in the OS keyring / encrypted
    # store (see secretstore.py).
    github_clone_root: str = ""
    github_auto_pull: bool = True
    github_auto_push: bool = False   # off by default: push happens on the Sync button, not every turn

    # Where the installable phone app (the mobile/ PWA) is published. Defaults to
    # this project's GitHub Pages site; editable for forks that publish elsewhere.
    phone_app_url: str = "https://pellkvistdev.github.io/Make-No-Mistakes/"

    extra: dict = field(default_factory=dict)

    def resolve_api_key(self) -> str:
        return os.environ.get("ZAI_API_KEY", "").strip() or self.api_key

    def resolve_tavily_key(self) -> str:
        return os.environ.get("TAVILY_API_KEY", "").strip() or self.tavily_api_key


THINKING_MODES = ("low", "medium", "high", "max")
# How many self-review-and-revise passes each mode runs after the main answer.
THINKING_REFINE_PASSES = {"low": 0, "medium": 0, "high": 1, "max": 3}


def load_config() -> Config:
    cfg = Config()
    data = {}
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
    # Configs written before thinking_mode existed only had the boolean
    # `thinking`: map it (off -> low, on -> medium). Then keep the two
    # consistent -- thinking is on for every mode except "low".
    if "thinking_mode" not in data or cfg.thinking_mode not in THINKING_MODES:
        cfg.thinking_mode = "medium" if cfg.thinking else "low"
    cfg.thinking = cfg.thinking_mode != "low"
    return cfg


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    extra = data.pop("extra", {})
    data.update(extra)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
