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

# The provider chosen during setup. Kept separate from Config.providers because
# it is the one whose key lives in an environment variable rather than the
# config file -- not because it is a better or more official provider. It used
# to be named "z.ai (free)" whatever it actually pointed at, which was wrong the
# moment anyone changed the base URL, and wronger once a second free option
# existed.
BUILTIN_PROVIDER_NAME = "z.ai (free)"   # legacy label; only for old configs


def provider_label(base_url: str, preset_key: str = "") -> str:
    """What to call a provider, based on what it actually is."""
    from . import providers as _providers
    found = (_providers.preset(preset_key)
             or _providers.preset_from_base_url(base_url))
    if found:
        return found["label"]
    # Typed in by hand: name it after the host rather than a vendor it is not.
    host = (base_url or "").split("//")[-1].split("/")[0]
    return host or "Your API"


def builtin_provider_name(cfg: "Config") -> str:
    """Legacy name of the provider chosen at setup.

    Only for reading configs and sessions written while there was such a thing
    as a primary provider. New code asks for `default_provider(cfg)`.
    """
    return provider_label(cfg.base_url, cfg.provider_preset)


# ---------------------------------------------------------------------- #
# Providers: ONE list, every entry the same kind of thing.
#
# It was not always. The provider picked at setup lived in cfg.base_url,
# cfg.model, cfg.vision_model and cfg.provider_preset, with its key in an
# environment variable; every other API lived in cfg.providers as a dict with
# its key in this file. Nothing ever decided that -- z.ai was hardwired in
# before a second provider was possible, and the list got added around it.
#
# The seam showed everywhere a person could touch it:
#   - the first row could only have its key replaced, while name, URL and
#     models were all editable on the others (save_provider short-circuited)
#   - the first row had no delete button, because delete_provider filtered
#     cfg.providers and the first row was not in it
#   - Settings had to offer a provider CHOICE, because "select the primary"
#     and "select a custom one" were different operations -- a second, rival
#     place to choose, next to a model picker that already listed everything
#
# So the setup provider is folded into the list on load and there is no
# primary afterwards. What remains is two pointers, each of them just a name:
# which model new chats start on, and which model reads images.

def _norm_models(entry: dict) -> None:
    """Fill in a provider's model lists so every row answers the same way.

    `models` is what to show by default and `all_models` is everything the
    endpoint listed. Rows added by hand only ever had `models`, so the UI had
    to guess which of the two it was looking at.
    """
    from . import providers as _providers
    every = [m for m in (entry.get("all_models") or entry.get("models") or []) if m]
    if not every:
        every = _providers.chat_models(entry.get("base_url", ""))
    shown = [m for m in (entry.get("models") or []) if m] or _providers.shortlist(every)
    entry["all_models"] = every or list(shown)
    entry["models"] = shown or list(every)


def normalize_provider(entry: dict) -> dict:
    """A provider dict with every field present, whatever wrote it."""
    from . import providers as _providers
    out = dict(entry or {})
    out["base_url"] = (out.get("base_url") or "").rstrip("/")
    known = (_providers.preset(out.get("preset") or "")
             or _providers.preset_from_base_url(out["base_url"]))
    out["preset"] = out.get("preset") or (known or {}).get("key", "")
    out["name"] = out.get("name") or provider_label(out["base_url"], out["preset"])
    # Where this provider's key lives. Per-provider and not one shared name:
    # configuring a second API must not overwrite the first one's key, and
    # reading a hosted key for a server running on this machine is how a key
    # ends up somewhere it was never meant to go.
    if "env_var" not in out:
        out["env_var"] = _providers.env_var_for(out["preset"]) if out["preset"] else ""
    out.setdefault("api_key", "")
    _norm_models(out)
    return out


def provider_key(entry: dict) -> str:
    """This provider's API key: its own environment variable first, then
    whatever was stored with it."""
    var = (entry or {}).get("env_var") or ""
    if var:
        found = os.environ.get(var, "").strip()
        if found:
            return found
    return (entry or {}).get("api_key", "") or ""


def all_providers(cfg: "Config") -> list:
    return [normalize_provider(p) for p in (cfg.providers or [])]


def find_provider(cfg: "Config", name: str) -> dict | None:
    if not name:
        return None
    for p in all_providers(cfg):
        if p.get("name") == name:
            return p
    # Chats saved before the setup provider was named after what it actually is
    # still refer to it as "z.ai (free)", and one typed in by hand is named
    # after its host ("localhost:11434") until the day it becomes a preset and
    # gains a label. Resolve both against the entry they now live in, rather
    # than reporting a provider that no longer exists -- those chats would
    # otherwise silently fall back and change model mid-conversation.
    legacy = (cfg.base_url or "").rstrip("/")
    host = legacy.split("//")[-1].split("/")[0]
    if name == BUILTIN_PROVIDER_NAME or (host and name == host):
        for p in all_providers(cfg):
            if p.get("base_url") == legacy:
                return p
    return None


def default_provider(cfg: "Config") -> dict | None:
    """Which API new chats start on."""
    return (find_provider(cfg, cfg.default_provider)
            or (all_providers(cfg) or [None])[0])


def default_model(cfg: "Config") -> str:
    """Which model new chats start on."""
    prov = default_provider(cfg)
    if not prov:
        return cfg.model
    if cfg.model and cfg.model in (prov.get("all_models") or []):
        return cfg.model
    # The saved default is not something this API serves -- it was retired, or
    # the default moved to another API. Anything that works beats a name that
    # 404s on the first message.
    return cfg.model or (prov.get("models") or [""])[0]


def vision_target(cfg: "Config", chat_provider: dict | None = None,
                  chat_model: str = "") -> tuple:
    """(provider, model) for reading images, or (None, "").

    Explicit beats automatic: a model named in Settings is used and nothing
    second-guesses it. That setting exists because the automatic answer used to
    be the whole story, and it was reached from the provider picked at setup --
    so a chat switched to another API had its images, and then its entire turn,
    handed back to the setup provider's model. Nothing about "I chose GLM for
    this chat" implies "read my images with Gemini".

    Automatic, in order: the chat's own model if its API reads images itself
    (nothing to route anywhere); failing that its API's dedicated vision model;
    failing that any configured API that can do either. (None, "") when nothing
    can, which is a real answer and not a reason to guess.
    """
    from . import providers as _providers
    named = find_provider(cfg, cfg.vision_provider)
    if named and cfg.vision_model:
        return named, cfg.vision_model
    chat_provider = chat_provider or default_provider(cfg)
    order = ([chat_provider] if chat_provider else []) + [
        p for p in all_providers(cfg) if p != chat_provider]
    for p in order:
        if not p:
            continue
        base = p.get("base_url", "")
        if _providers.is_multimodal(base):
            model = (chat_model if p is chat_provider and chat_model
                     else (p.get("models") or [""])[0])
            if model:
                return p, model
        own = _providers.vision_model_for(base)
        if own:
            return p, own
    return None, ""


def _migrate_primary_into_providers(cfg: "Config") -> None:
    """Fold the setup provider into the provider list, once.

    Everything about it was already provider-shaped; it was only ever stored
    apart. Deliberately keyed on base_url rather than name, because the name
    has changed at least twice ("z.ai (free)", then the preset's label, then
    the host for hand-typed ones) and matching on it would add a duplicate row
    for the same endpoint.
    """
    base = (cfg.base_url or "").rstrip("/")
    if not base:
        return
    for p in cfg.providers:
        if (p.get("base_url") or "").rstrip("/") == base:
            return                      # already migrated
    entry = normalize_provider({
        "name": builtin_provider_name(cfg),
        "base_url": base,
        "preset": cfg.provider_preset or "",
        # cfg.api_key is the "the environment write was blocked" fallback, and
        # stays exactly that: provider_key reads the variable first.
        "api_key": cfg.api_key or "",
        "all_models": list(cfg.available_models),
    })
    if cfg.model and cfg.model not in entry["models"]:
        # In use, so it stays selectable whatever any catalogue says.
        entry["models"] = [cfg.model] + entry["models"]
        if cfg.model not in entry["all_models"]:
            entry["all_models"] = [cfg.model] + entry["all_models"]
    # First, because it is the one the person set up and expects to see at the
    # top -- ordering is presentation now, not meaning.
    cfg.providers.insert(0, entry)
    if not cfg.default_provider:
        cfg.default_provider = entry["name"]


def _migrate_vision_pointer(cfg: "Config") -> None:
    """Drop a vision model nobody chose.

    cfg.vision_model looks like a setting and never was one: setup wrote
    `preset["vision_model"] or model` into it and no screen could change it.
    Carrying that forward as an explicit pin would recreate the bug this
    rework is for -- on a Google install it equals the chat model, so pinning
    it says "read every image with Gemini" and a chat switched to z.ai has its
    images sent back to Google, which is exactly what it must stop doing.

    So an unowned value becomes `auto`, which now resolves per chat. Anything
    with an owner was set deliberately, on the screen that exists for it, and
    is left alone.
    """
    if cfg.vision_provider:
        return
    cfg.vision_model = ""


@dataclass
class Config:
    api_key: str = ""  # legacy only; the real source is the provider's env var
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    vision_model: str = DEFAULT_VISION_MODEL
    # Which known provider the primary one is ("zai", "google", "" = typed in
    # by hand). Only ever a label and a set of instructions: the client treats
    # every provider the same, and this changes nothing about how it is called.
    provider_preset: str = ""
    # Which API new chats start on, by name. Replaces "the primary provider",
    # which was not a choice anybody made -- it was wherever the setup screen
    # happened to write, and the only row that could not be edited or removed.
    default_provider: str = ""
    # Which API serves cfg.vision_model. "" means work it out (see
    # vision_target). The model name alone used to be the whole setting, and it
    # implicitly meant "on the setup provider" -- which is how a chat moved to
    # z.ai had its images, and then its whole turn, sent back to Gemini.
    vision_provider: str = ""
    # Has this install been through setup? Deliberately NOT inferred from
    # "can a key be found anywhere", which is what the first-run check used to
    # ask. The key is persisted with `setx`, i.e. into HKCU\Environment in the
    # registry -- it does not live in this folder, or in the app's folder, or
    # anywhere else an uninstall or a delete can reach. So deleting
    # ~/.makenomistakes and reinstalling left the key behind, the old check
    # found it, and setup never appeared: the one action everybody tries when
    # something is wrong was also the one the app quietly ignored.
    setup_done: bool = False
    # What the primary endpoint said it can serve, last time it was asked.
    # A catalogue of model names cannot stay right -- gemini-2.5-flash was the
    # documented default and then answered
    #   404 no longer available to new users
    # on a key issued a week later. The key is the only thing that knows, so
    # the catalogue expresses a PREFERENCE and this holds the reality.
    available_models: list = field(default_factory=list)
    mode: str = "ask"                # ask | autoedit | yolo
    temperature: float = 0.6
    max_tokens: int = 16384
    thinking: bool = True            # GLM reasoning mode (derived from thinking_mode; kept for compat)
    thinking_mode: str = "medium"    # low | medium | high | max (effort/iteration level)
    verify_edits: bool = False       # nudge the agent to verify edits it never ran anything to check (off by default)
    auto_fix_tests: bool = False     # "make it green": after an edit turn, run the project's tests and fix until they pass (opt-in, bounded)
    codebase_memory_neural: bool = False  # search_code uses a local embedding model (semantic) instead of lexical TF-IDF
    # Record the tool failures this model makes in this project, and put the
    # repeat offenders in its system prompt (glmcode/ledger.py). On by default
    # because it is passive -- it observes a funnel every tool result already
    # goes through -- and capped hard, so the prompt prefix it competes with
    # cannot grow without bound. Off means neither record nor inject.
    learn_from_mistakes: bool = True
    parallel_attempts: int = 1       # "race": 1 = off; 2 or 3 = run that many isolated attempts from a common baseline and keep the best
    show_reasoning: bool = True      # print the model's reasoning (dimmed)
    # auto = ask the provider (see Agent._images_go_direct). It used to
    # default to "describe", so a model that reads images perfectly well was
    # handed somebody else's prose about them instead -- and the agent called
    # view_image on a picture the user had just attached.
    vision_route: str = "auto"       # auto | describe | direct
    # Auto-compact above this. The GLM models carry ~200k; the headroom is for
    # the reply, not for the counter being wrong -- the estimate now calibrates
    # itself against the prompt_tokens the API reports (api.calibrate_ratio),
    # so it no longer has to be padded against a guess the way chars/N did.
    context_limit_tokens: int = 185_000
    max_turns_per_request: int = 200  # safety cap on agentic iterations
    search_provider: str = "auto"    # auto | ddg | tavily
    tavily_api_key: str = ""         # optional upgrade: free key at tavily.com
    background_path: str = ""        # desktop app: custom background image
    gui_cwd: str = ""                # unused (kept for old config compatibility)
    last_session_id: str = ""        # desktop app: session to resume on launch
    read_aloud: bool = False         # desktop app: auto-speak assistant replies (Kokoro TTS)
    notifications: bool = True       # desktop app: OS toasts while the window is unfocused
    # Finish turns a phone was suspended part-way through. On by default: the
    # phone cannot do this for itself (iOS gives web content no background
    # execution at all), so leaving it off means the work is simply lost.
    sync_finish_interrupted: bool = True
    reduce_effects: bool = True      # desktop app: blur-free fast theme (default); glass is opt-in
    browser_headless: bool = False   # control_chrome: hide the browser window (default: watch it)
    browser_keep_logins: bool = False  # control_chrome: persistent agent profile (logins survive restarts)
    # control_chrome: attach to a browser the USER is already running (CDP
    # endpoint, e.g. http://localhost:9222). "" = launch our own, which is
    # the default and the safe one -- attaching hands the agent their live
    # logged-in session.
    # control_chrome: drive the browser the user already has open, through
    # the extension. The one that needs no relaunch, so it is the one the
    # UI offers; browser_connect_url stays for the DevTools-port route.
    # control_chrome and the user's own browser. "auto" -- the default -- means
    # the extension is used whenever it is connected: installing an unpacked
    # extension into your own browser is already a deliberate act, and asking
    # for a second opt-in afterwards is how someone ends up with everything set
    # up and nothing working. "off" is the way out.
    #
    # A NEW field rather than flipping browser_use_mine's default: that one
    # defaulted to False, so a persisted False is indistinguishable from a
    # choice, and every existing install would have stayed off.
    browser_own: str = "auto"
    # Models to fall back to, in order, when the chat's model is rate-limited.
    # Same endpoint and key -- only the model name changes -- because that is
    # the shape the free tiers have: one API, several models, one quota each.
    model_fallbacks: list = field(default_factory=list)
    browser_connect_url: str = ""
    browser_provider: str = ""       # control_chrome: dedicated Browser Agent provider ("" = same as chat)
    browser_model: str = ""          # control_chrome: dedicated Browser Agent model ("" = same as chat)
    # How voice mode runs. "local" is the default and stays it: Whisper and
    # Kokoro work offline, cost nothing, and send no audio anywhere, which is a
    # real feature and not just the older way. "live" hands the whole
    # conversation to a speech-to-speech model over a WebSocket -- lower
    # latency and real barge-in, but it needs a key, a network, and quota.
    voice_engine: str = "local"      # local | live
    live_voice: str = "Puck"         # prebuilt Live API voice name
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

    def __post_init__(self) -> None:
        # Not only in load_config. A Config is built directly by the CLI, by
        # tests, and by anything that wants defaults, and every one of those
        # would otherwise see a provider list missing the provider the app is
        # actually configured to use. Idempotent: it matches on base_url and
        # returns immediately once the row is there.
        _migrate_primary_into_providers(self)
        # Keeps one invariant true everywhere: a vision model is only ever set
        # together with the API that serves it. A bare name is a leftover from
        # when there was only one API it could have meant.
        _migrate_vision_pointer(self)

    def provider_env_var(self) -> str:
        """Which environment variable holds the primary provider's key.

        Per-preset, so configuring Google does not overwrite a z.ai key and
        vice versa. Falls back to ZAI_API_KEY for installs that predate presets,
        whose key is already sitting in that variable.
        """
        from . import providers as _providers
        key = self.provider_preset or ""
        if not key:
            found = _providers.preset_from_base_url(self.base_url)
            key = found["key"] if found else ""
        if not key:
            # Nothing identifiable: a config from before presets, whose key is
            # in ZAI_API_KEY under a name no migration could reach.
            return "ZAI_API_KEY"
        # "" for a preset that takes no key (Ollama). It must NOT fall through
        # to ZAI_API_KEY -- that would read a z.ai key and send it to a server
        # on this machine, which is the leak this function used to have by a
        # different route.
        return _providers.env_var_for(key)

    def resolve_api_key(self) -> str:
        var = self.provider_env_var()
        key = os.environ.get(var, "").strip() if var else ""
        if key:
            return key
        # ZAI_API_KEY is a MIGRATION PATH, not a general fallback, and the
        # difference matters: it used to be consulted for every provider, so
        # choosing one could hand it another's key. Pick "Other", point it at
        # a local Ollama or an endpoint you typed, leave the key box empty as
        # a local server invites you to -- and the z.ai key went to that
        # endpoint. Same for Google on a machine where `setx` is blocked by
        # policy: GOOGLE_API_KEY never persists, this fallback fires ahead of
        # the stored api_key, and requests fail 401 holding the wrong key.
        #
        # An empty provider_preset is the marker for a config written before
        # presets existed, whose key really is sitting in ZAI_API_KEY under a
        # name nothing could migrate. Once a provider has been chosen
        # explicitly, only its own variable and the stored key count.
        if not self.provider_preset:
            legacy = os.environ.get("ZAI_API_KEY", "").strip()
            if legacy:
                return legacy
        return self.api_key

    def resolve_tavily_key(self) -> str:
        return os.environ.get("TAVILY_API_KEY", "").strip() or self.tavily_api_key


THINKING_MODES = ("low", "medium", "high", "max")
# How many self-review-and-revise passes each mode runs after the main answer.
THINKING_REFINE_PASSES = {"low": 0, "medium": 0, "high": 1, "max": 3}


def _model_is_stale(model: str, preset: dict, listed: set, retired: set,
                    known: set) -> bool:
    """Is this model one nothing still recommends?

    In order of authority: a model the vendor is known to refuse to new keys is
    stale whatever else says; failing that, the endpoint's own listing decides;
    and with neither, the catalogue is all there is.
    """
    if not model:
        return False
    if model in retired:
        return True
    if listed:
        return model not in listed
    return model not in known


def _retire_stale_model(cfg: "Config") -> None:
    """Move a saved config off a model its own preset has dropped.

    Updating a preset's default only ever changed what a NEW install picks.
    Anyone already set up kept the model in their config file -- so every new
    chat went on choosing gemini-2.5-flash long after the preset stopped
    recommending it and Google stopped serving it to new keys. The fix has to
    reach the config, not just the catalogue.

    Deliberately narrow. It fires only when the provider is a known preset AND
    the saved model is absent from BOTH the preset's list and whatever the
    endpoint last said it serves -- i.e. nothing that knows anything still
    lists it. A model typed in by hand, or one the provider confirms exists, is
    left alone: overruling a deliberate choice is worse than the staleness.
    """
    from . import providers as _providers
    p = _providers.preset(cfg.provider_preset)
    if not p or not cfg.model:
        return
    known = set(p.get("chat_models") or p.get("models") or [])
    listed = set(cfg.available_models or [])
    retired = set(p.get("retired_models") or [])
    if not _model_is_stale(cfg.model, p, listed, retired, known):
        return
    replacement = p.get("model") or ""
    if not replacement or replacement == cfg.model:
        return
    cfg.model = replacement
    # The vision model rides along only if it is stale by the SAME rule -- it
    # is usually the identical string, and leaving it behind would point image
    # work at the model that was just retired.
    if _model_is_stale(cfg.vision_model, p, listed, retired, known):
        cfg.vision_model = p.get("vision_model") or replacement


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
    # A config file that predates this field belongs to someone who has already
    # been through setup -- there was no other way to get one. Without this
    # they would all be asked again on the next update, which is a worse bug
    # than the one setup_done exists to fix.
    if "setup_done" not in data:
        cfg.setup_done = CONFIG_FILE.exists()
    _retire_stale_model(cfg)
    # Before anything reads cfg.providers: until this runs the list is missing
    # the provider most installs actually use.
    _migrate_primary_into_providers(cfg)
    _migrate_vision_pointer(cfg)
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
