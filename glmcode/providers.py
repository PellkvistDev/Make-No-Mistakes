"""Known model providers, and what a person needs to know to pick one.

Every provider this app talks to is an OpenAI-compatible endpoint, so a preset
is only ever a base URL, some model names, and the instructions for getting a
key. Nothing here is special-cased in the client: a preset produces the same
provider dict a hand-typed "Other" produces, and the agent cannot tell them
apart. That is the point -- z.ai was hardwired into the config as *the* provider
(DEFAULT_BASE_URL, a ZAI_API_KEY env var, a provider literally named "z.ai
(free)"), which made adding a second free option a special case on top of a
special case.

What belongs in a preset is the part that is genuinely per-provider and that a
user would otherwise have to go and find out: where the key comes from, what to
type in the box, which model to start on, and anything about the free tier that
they would be annoyed to discover afterwards.
"""

# Where each preset keeps its key. Per-preset rather than one shared name, so
# configuring a second provider cannot silently overwrite the first one's key.
_ENV_VARS = {
    "zai": "ZAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    # Typed in by hand, so there is no vendor to name it after. Deliberately
    # not ZAI_API_KEY: an endpoint someone pasted is not z.ai, and reusing that
    # name is how the config ended up believing everything was.
    "custom": "MNM_API_KEY",
}

PRESETS = [
    {
        "key": "zai",
        "label": "Z.AI",
        "base_url": "https://api.z.ai/api/paas/v4",
        "model": "glm-4.7-flash",
        "vision_model": "glm-4.6v-flash",
        "models": ["glm-4.7-flash", "glm-4.6v-flash"],
        "free_models": ["glm-4.7-flash", "glm-4.6v-flash"],
        "env_var": "ZAI_API_KEY",
        "key_url": "https://z.ai/manage-apikey/apikey-list",
        "blurb": "GLM coding models. A free tier with no card required.",
        "free": "glm-4.7-flash and the vision model are free to use.",
        # Nothing to warn about that is worse than any other hosted API.
        "caveat": "",
        "steps": [
            "Open z.ai and sign in (or create an account).",
            "Go to API Keys and create a key.",
            "Copy it and paste it below.",
        ],
    },
    {
        "key": "google",
        "label": "Google AI Studio",
        # The OpenAI-compatible surface, NOT the native generateContent one:
        # same /chat/completions shape and a Bearer key, so it needs no client
        # of its own. Tool calls and streaming both work here, which matters
        # because this app is a tool-calling loop and would be useless without.
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        # Flash rather than Flash-Lite deliberately: the Lite models are cheaper
        # and faster but do not reliably stream tool-call arguments, and every
        # turn here is tool calls.
        "model": "gemini-2.5-flash",
        "vision_model": "gemini-2.5-flash",
        # Pro is offered because it is a real option on this key and a better
        # model -- but it is NOT free, and has not been since Google moved the
        # Pro models behind billing. Listing it without saying so would make
        # the free-tier line above cover something it does not.
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "free_models": ["gemini-2.5-flash"],
        "env_var": "GOOGLE_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "blurb": "Gemini models. A free tier with no card required.",
        # No specific quota numbers. Google cut the free allowances sharply at
        # the end of 2025 and no longer publishes one table that applies to
        # everyone -- a figure hardcoded here would quietly become a lie, and
        # the console is the only place that knows the truth for this key.
        "free": "Flash is free. Your exact quota is shown in AI Studio.",
        # Said plainly and shown next to the word "free", because it is the one
        # thing about this option that someone might mind and would otherwise
        # only find out later. This app sends source code.
        "caveat": (
            "On the free tier Google may use your prompts to improve their "
            "models. Your code is part of that. Paid keys are excluded."
        ),
        "steps": [
            "Open Google AI Studio and sign in with a Google account.",
            "Click Get API key, then Create API key.",
            "Copy it and paste it below.",
        ],
    },
]

# Not a preset: the escape hatch. Anything OpenAI-compatible -- OpenRouter, a
# paid OpenAI or Anthropic-compatible gateway, Ollama or LM Studio on this
# machine -- is typed in by hand and works identically.
CUSTOM_KEY = "custom"


def preset(key: str) -> dict | None:
    for p in PRESETS:
        if p["key"] == key:
            return p
    return None


def preset_keys() -> list:
    return [p["key"] for p in PRESETS]


def env_var_for(key: str) -> str:
    """Which environment variable holds this preset's key."""
    return _ENV_VARS.get(key, "")


def preset_from_base_url(base_url: str) -> dict | None:
    """Which preset (if any) a configured base URL belongs to.

    Needed because installs that predate presets have only a base_url in their
    config: without this they would all be labelled "custom" and lose their
    instructions and their key.
    """
    url = (base_url or "").strip().rstrip("/").lower()
    if not url:
        return None
    for p in PRESETS:
        if p["base_url"].rstrip("/").lower() == url:
            return p
    return None


def to_provider(key: str, api_key: str = "") -> dict | None:
    """A preset -> the provider dict the rest of the app already understands.

    Deliberately the same shape a hand-typed provider produces. A preset is a
    convenience at the point of typing, not a different kind of thing
    afterwards.
    """
    p = preset(key)
    if not p:
        return None
    return {
        "name": p["label"],
        "base_url": p["base_url"],
        "api_key": api_key or "",
        "models": list(p["models"]),
        "preset": p["key"],
    }


def choices() -> list:
    """Everything the setup screen needs to draw itself, presets + Other.

    Returned as data rather than built in the UI so that the desktop and the
    phone show the same options, in the same order, with the same wording --
    they are separate programs and this is the only thing keeping them honest.
    """
    out = []
    for p in PRESETS:
        c = {k: p[k] for k in
             ("key", "label", "base_url", "model", "models", "key_url",
              "blurb", "free", "caveat", "steps")}
        # Per model, not just per provider. A provider is not simply "free" or
        # not: Google's Flash is free and its Pro is not, and a list that shows
        # them side by side under a heading saying "free tier" is how someone
        # picks the paid one by accident.
        free = set(p.get("free_models") or [])
        c["model_options"] = [{"name": m, "free": m in free} for m in p["models"]]
        out.append(c)
    out.append({
        "key": CUSTOM_KEY,
        "label": "Other",
        "base_url": "",
        "model": "",
        "models": [],
        "key_url": "",
        "blurb": "Any OpenAI-compatible API, or a model running on this machine.",
        "free": "",
        "caveat": "",
        "model_options": [],
        "steps": [
            "Paste the API's base URL (the part ending in /v1 or similar).",
            "Paste a key if it needs one — local servers usually do not.",
            "Type the model name exactly as the provider spells it.",
        ],
    })
    return out
