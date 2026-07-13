"""Interactive REPL for GLM Code: input handling, slash commands, image detection."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from . import __version__, ui
from .agent import Agent
from .api import IMAGE_EXTENSIONS, ZaiClient
from .config import (CONFIG_DIR, HISTORY_FILE, PERMISSION_MODES, Config,
                     load_config, save_config)
from .tools import clear_todos, configure_search
from .permissions import add_command_aliases

PROMPT_STYLE = Style.from_dict({"prompt": "ansicyan bold"})

HELP = """\
Commands:
  /help                 show this help
  /mode [ask|autoedit|yolo]   show or set the permission mode
                          ask      = approve file writes & commands (default)
                          autoedit = file edits auto-approved, commands ask
                          yolo     = everything auto-approved
  /model [id]           show or set the chat model (default glm-4.7-flash)
  /vision [describe|direct]   how images are handled:
                          describe = vision model describes image for the coding
                                     model (default, keeps best coding quality)
                          direct   = whole turn runs on the vision model
  /image <path> [text]  attach an image file with an optional question
  /think [on|off]       toggle model reasoning; /reasoning [on|off] toggles display
  /clear                start a fresh conversation
  /compact              summarize history into a compact context
  /cost                 show token usage (everything is free)
  /config               show config; /config key value to set (saved to disk)
  /init                 ask the agent to generate a GLM.md project memory file
  /exit or /quit        leave (Ctrl+D also works)

Input:
  Esc+Enter or Alt+Enter inserts a newline; Enter sends.
  Paths to .png/.jpg/... images in your message are attached automatically.
  Ctrl+C interrupts the agent mid-turn.
"""

INIT_PROMPT = (
    "Explore this project (list_dir, read key files like README/package manifests, "
    "glob for source layout) and then create a GLM.md file in the project root. "
    "It should briefly document: what the project is, the layout of important "
    "directories, how to build/run/test it (exact commands), and any conventions "
    "you can infer. Keep it under 60 lines. This file is loaded as your own "
    "instructions in future sessions, so write it for yourself."
)

IMAGE_PATH_RE = re.compile(
    r"\"([^\"]+?\.(?:png|jpe?g|gif|webp|bmp))\"|'([^']+?\.(?:png|jpe?g|gif|webp|bmp))'"
    r"|(\S+?\.(?:png|jpe?g|gif|webp|bmp))\b",
    re.IGNORECASE,
)


def extract_images(text: str) -> tuple[str, list[Path]]:
    """Find existing image-file paths mentioned in the message."""
    images: list[Path] = []
    def repl(m: re.Match) -> str:
        raw = next(g for g in m.groups() if g)
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(p.resolve())
            return f"[attached image: {p.name}]"
        return m.group(0)
    cleaned = IMAGE_PATH_RE.sub(repl, text)
    return (cleaned if images else text), images


# --------------------------------------------------------------------- #

def persist_env_var(name: str, value: str) -> bool:
    """Set an environment variable for this process AND persist it for the user
    (Windows user environment, survives new terminals). Returns True if persisted."""
    import os
    import subprocess
    os.environ[name] = value
    if os.name != "nt":
        return False
    from .tools import NO_WINDOW_KWARGS
    try:
        r = subprocess.run(["setx", name, value], capture_output=True, timeout=15,
                           **NO_WINDOW_KWARGS)
        return r.returncode == 0
    except OSError:
        return False


def ensure_api_key(cfg: Config) -> str:
    import os
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key

    # legacy: key stored in config.json by an older version -> migrate to env var
    if cfg.api_key:
        key = cfg.api_key
        persisted = persist_env_var("ZAI_API_KEY", key)
        cfg.api_key = ""
        save_config(cfg)
        ui.info("migrated your API key from config.json to the ZAI_API_KEY "
                "user environment variable" if persisted else
                "using legacy key from config.json (set ZAI_API_KEY to override)")
        return key

    ui.console.print(
        "\n[bold]Welcome to GLM Code![/] The [bold]ZAI_API_KEY[/] environment "
        "variable is not set.\n"
        "  1. Sign up at [cyan]https://z.ai[/] (free, no credit card)\n"
        "  2. Open your profile menu -> [bold]API Keys[/] -> create a key\n"
        "  3. Paste it below and it will be saved as a user environment\n"
        "     variable (ZAI_API_KEY), or set it yourself and restart:\n"
        "       [dim]setx ZAI_API_KEY your-key-here[/]\n",
        highlight=False,
    )
    try:
        key = input("  API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        ui.error("No API key provided; exiting.")
        sys.exit(1)
    if persist_env_var("ZAI_API_KEY", key):
        ui.info("saved as ZAI_API_KEY user environment variable "
                "(already-open terminals need a restart to see it)")
    else:
        ui.warn("could not persist ZAI_API_KEY automatically; it is set for this "
                "session only. Persist it with: setx ZAI_API_KEY your-key")
    return key


def make_session() -> PromptSession:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    kb = KeyBindings()

    @kb.add("escape", "enter")  # Esc+Enter / Alt+Enter -> newline
    def _(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        key_bindings=kb,
        style=PROMPT_STYLE,
        multiline=False,
        enable_open_in_editor=False,
    )


# --------------------------------------------------------------------- #
# Slash commands

def handle_slash(cmd: str, agent: Agent, cfg: Config) -> bool:
    """Returns True if the input was handled as a command."""
    parts = cmd.split(None, 2)
    name = parts[0].lower()

    if name in ("/exit", "/quit"):
        raise SystemExit(0)

    if name == "/help":
        ui.console.print(HELP, highlight=False)
        return True

    if name == "/mode":
        if len(parts) > 1 and parts[1] in PERMISSION_MODES:
            agent.set_mode(parts[1])
            save_config(cfg)
            ui.info(f"mode set to {parts[1]}")
        else:
            ui.info(f"mode: {cfg.mode} (options: {', '.join(PERMISSION_MODES)})")
        return True

    if name == "/model":
        if len(parts) > 1:
            cfg.model = parts[1]
            agent.rebuild_system_prompt()
            save_config(cfg)
            ui.info(f"model set to {cfg.model}")
        else:
            ui.info(f"model: {cfg.model} | vision: {cfg.vision_model}")
        return True

    if name == "/vision":
        if len(parts) > 1 and parts[1] in ("describe", "direct"):
            cfg.vision_route = parts[1]
            save_config(cfg)
        ui.info(f"vision routing: {cfg.vision_route} "
                "(describe = vision model narrates for the coding model; "
                "direct = vision model runs the whole turn)")
        return True

    if name == "/think":
        cfg.thinking = _toggle(parts, cfg.thinking)
        save_config(cfg)
        ui.info(f"model reasoning: {'on' if cfg.thinking else 'off'}")
        return True

    if name == "/reasoning":
        cfg.show_reasoning = _toggle(parts, cfg.show_reasoning)
        save_config(cfg)
        ui.info(f"reasoning display: {'on' if cfg.show_reasoning else 'off'}")
        return True

    if name == "/clear":
        agent.clear()
        clear_todos()
        ui.info("conversation cleared")
        return True

    if name == "/compact":
        ui.info(agent.compact())
        return True

    if name == "/cost":
        u = agent.session_usage
        ui.usage_line(u.prompt_tokens, u.completion_tokens, agent.context_estimate())
        return True

    if name == "/config":
        if len(parts) >= 3:
            key, value = parts[1], parts[2]
            if hasattr(cfg, key) and key != "extra":
                old = getattr(cfg, key)
                cast = type(old)
                try:
                    setattr(cfg, key, cast(value) if cast is not bool
                            else value.lower() in ("1", "true", "on", "yes"))
                    save_config(cfg)
                    if key in ("search_provider", "tavily_api_key"):
                        configure_search(cfg.search_provider, cfg.resolve_tavily_key())
                    ui.info(f"{key} = {getattr(cfg, key)}")
                except (TypeError, ValueError) as e:
                    ui.error(f"bad value: {e}")
            else:
                ui.error(f"unknown config key: {key}")
        else:
            for k in ("model", "vision_model", "mode", "temperature", "max_tokens",
                      "thinking", "vision_route", "context_limit_tokens", "base_url",
                      "search_provider"):
                ui.info(f"{k} = {getattr(cfg, k)}")
            import os
            src = ("ZAI_API_KEY env var" if os.environ.get("ZAI_API_KEY", "").strip()
                   else "config.json (legacy)" if cfg.api_key else "MISSING")
            ui.info(f"api_key = {src}")
            ui.info(f"tavily_api_key = "
                    f"{'(set)' if cfg.resolve_tavily_key() else '(not set; using free DuckDuckGo)'}")
        return True

    if name == "/init":
        agent.run_turn({"role": "user", "content": INIT_PROMPT})
        return True

    if name == "/image":
        if len(parts) < 2:
            ui.error("usage: /image <path> [question]")
            return True
        img = Path(parts[1].strip('"')).expanduser()
        if not img.is_absolute():
            img = Path.cwd() / img
        if not img.is_file():
            ui.error(f"image not found: {img}")
            return True
        text = parts[2] if len(parts) > 2 else "The user attached this image."
        try:
            msg = agent.attach_images(text, [img.resolve()])
        except Exception as e:
            ui.error(f"could not process image: {e}")
            return True
        agent.run_turn(msg)
        return True

    ui.error(f"unknown command: {name} (try /help)")
    return True


def _toggle(parts: list[str], current: bool) -> bool:
    if len(parts) > 1:
        return parts[1].lower() in ("on", "true", "1", "yes")
    return not current


# --------------------------------------------------------------------- #

def main() -> None:
    if "--version" in sys.argv:
        print(f"GLM Code v{__version__}")
        return

    cfg = load_config()
    api_key = ensure_api_key(cfg)
    configure_search(cfg.search_provider, cfg.resolve_tavily_key())
    # Initialize command aliases for npm/yarn/pnpm/git
    add_command_aliases({
        "npm": "npm",
        "yarn": "npm",
        "pnpm": "npm",
        "git": "git",
    })
    client = ZaiClient(api_key, cfg.base_url)
    agent = Agent(cfg, client)

    ui.banner(cfg.model, cfg.vision_model, cfg.mode, str(Path.cwd()), __version__)

    # non-interactive: glm -p "prompt"
    if "-p" in sys.argv:
        idx = sys.argv.index("-p")
        prompt = " ".join(sys.argv[idx + 1:])
        if prompt:
            agent.run_turn({"role": "user", "content": prompt})
        return

    session = make_session()
    while True:
        try:
            text = session.prompt([("class:prompt", "> ")]).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not text:
            continue

        if text.startswith("/"):
            try:
                handle_slash(text, agent, cfg)
            except SystemExit:
                break
            continue

        cleaned, images = extract_images(text)
        try:
            if images:
                ui.info("attaching: " + ", ".join(p.name for p in images))
                msg = agent.attach_images(cleaned, images)
            else:
                msg = {"role": "user", "content": text}
            agent.run_turn(msg)
        except KeyboardInterrupt:
            ui.warn("interrupted")
        except Exception as e:
            ui.error(f"{type(e).__name__}: {e}")

    u = agent.session_usage
    if u.total:
        ui.usage_line(u.prompt_tokens, u.completion_tokens, agent.context_estimate())
    ui.info("bye!")
