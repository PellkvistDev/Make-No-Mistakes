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
    # Ollama has no key and no account, so it gets no variable. Deliberately
    # "" rather than absent: env_var_for() returns "" either way, but a
    # provider that is *known* to need no key is a different thing from one
    # nobody has thought about, and config.provider_env_var() relies on the
    # difference to avoid falling back to ZAI_API_KEY for it.
    "ollama": "",
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
        # Which of those are a chat choice. glm-4.6v-flash is the vision model
        # -- it is routed to automatically for images and picking it to code
        # with would be a mistake, so it is offered as a model but not as a
        # model to hold a conversation in.
        "chat_models": ["glm-4.7-flash"],
        "free_models": ["glm-4.7-flash", "glm-4.6v-flash"],
        "env_var": "ZAI_API_KEY",
        # Non-standard request fields this endpoint understands. `thinking` is
        # a Zhipu extension, not part of the OpenAI schema, and Google's
        # compatibility layer rejects the whole request over it:
        #   400 Unknown name "thinking": Cannot find field.
        # It was sent to everything, because when it was added there was only
        # one place it could go.
        "extensions": ["thinking"],
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
        # PREFERENCE ORDER, not a promise that any of these exist. The live
        # list from `GET /models` decides (see preferred_model), so a name
        # going stale here costs nothing -- which is the only reason it is safe
        # to write current model names down at all.
        #
        # Newest first, because Google retires models ahead of their published
        # shutdown dates: gemini-2.5-flash answered "no longer available to new
        # users" months before its announced 16 Oct 2026 shutdown, and the same
        # is reported for 2.5 Flash-Lite and 2.5 Pro. The 2.5 entries stay at
        # the back for keys that still have access to them.
        "models": ["gemini-3.6-flash", "gemini-3.5-flash",
                   "gemini-3.5-flash-lite", "gemini-3.1-pro",
                   "gemini-2.5-flash", "gemini-2.5-pro"],
        "chat_models": ["gemini-3.6-flash", "gemini-3.5-flash",
                        "gemini-3.5-flash-lite", "gemini-3.1-pro",
                        "gemini-2.5-flash", "gemini-2.5-pro"],
        # No per-model free/paid claims. Which models a free tier covers has
        # changed repeatedly, and a label here would be a guess about someone
        # else's billing attached to a model name that may not outlive it.
        "free_models": [],
        # Pro is deliberately neither "free" nor "paid" here. Whether the free
        # tier covers it has changed more than once -- it was 5 RPM / 100 RPD
        # in early 2026, and there are reports of it moving behind billing
        # since -- and this file cannot be right about it for long. Only the
        # key itself knows, so the app says where to look instead of claiming.
        "unsure_models": [],
        # Gemini 3 returns a thought_signature on every tool call and requires
        # it back on each following request. Declared as an extension so it is
        # sent here and stripped for everyone else, exactly like z.ai's
        # `thinking` -- the two are the same problem pointing opposite ways.
        "extensions": ["thought_signature"],
        "env_var": "GOOGLE_API_KEY",
        "key_url": "https://aistudio.google.com/apikey",
        "blurb": "Gemini models. A free tier with no card required.",
        # No specific quota numbers. Google cut the free allowances sharply at
        # the end of 2025 and no longer publishes one table that applies to
        # everyone -- a figure hardcoded here would quietly become a lie, and
        # the console is the only place that knows the truth for this key.
        # No model named and no quota quoted. "Flash is free" was true when it
        # was written and then the model itself was withdrawn; AI Studio is the
        # only place that knows what this key gets today.
        "free": "There is a free tier. AI Studio shows which models it covers "
                "and what your quota is.",
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
    {
        "key": "ollama",
        "label": "Ollama (on this PC)",
        # Ollama serves an OpenAI-compatible API on this path, so it needs no
        # more special-casing than any hosted provider: same /chat/completions,
        # same tool calls, no client of its own.
        "base_url": "http://localhost:11434/v1",
        # Empty on purpose, and the only preset for which that is true. Which
        # models exist depends on what has been pulled onto this machine, so
        # naming one here would be a guess -- and a guess that fails at the
        # first request rather than at setup. The models are read from the
        # running server instead (Api.local_models).
        "model": "",
        "vision_model": "",
        "models": [],
        "free_models": [],
        "unsure_models": [],
        # No key, no account. env_var_for() returns "" and setup does not ask.
        "env_var": "",
        "needs_key": False,
        # Nothing to send them to for a key; the link is the download.
        "key_url": "https://ollama.com/download",
        "blurb": "Runs on your own machine. No account, no key, no limits.",
        # The one genuinely unconditional "free" in this file: there is no
        # quota to run out of, no tier to be moved off, and no policy that can
        # change next month, because there is no company in the loop.
        "free": "Free, always, and your code never leaves this PC.",
        # Said because it is the real trade and it is not obvious to someone
        # comparing three "free" options: local models are much weaker than
        # the hosted ones, and the machine has to be up to it.
        "caveat": (
            "Quality depends on your hardware. A small local model will not "
            "match the hosted options above at hard coding work."
        ),
        "steps": [
            "Install Ollama from ollama.com and let it start.",
            "Pull a coding model, e.g.  ollama pull qwen2.5-coder",
            "Come back here — it finds what you have installed.",
        ],
        # Offered when the server is up but empty, so the fix is a command to
        # copy rather than a question about which model to choose.
        "suggest_pull": "qwen2.5-coder",
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


def is_local(base_url: str) -> bool:
    """Is this endpoint a server on the user's own machine?

    Worth knowing separately from the presets: a model running locally is free
    in a way no hosted free tier is -- there is no quota, no account, and no
    policy that can change next month.
    """
    host = (base_url or "").split("//")[-1].split("/")[0].lower()
    # Strip the port -- but an IPv6 literal is full of colons and is bracketed
    # for exactly that reason, so splitting on ":" first would leave "[".
    if host.startswith("["):
        host = host[1:].split("]")[0]
    else:
        host = host.split(":")[0]
    return host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def model_tier(base_url: str, model: str) -> str:
    """What can honestly be said about this model's price: one of

    "local"  -- runs on this machine, so it costs nothing and cannot change.
    "free"   -- the catalogue says this provider's free tier covers it.
    "unsure" -- known model, but whether the free tier covers it is not
                something this file can be right about (see gemini-2.5-pro).
    ""       -- no idea. A hand-typed endpoint could be anything, and the app
                has no way to find out.

    The empty string is the default on purpose. Every price this app displays
    used to be the literal text "$0.00", written when z.ai was the only
    provider it could talk to; the moment a second one existed that text was a
    claim about someone else's billing that nobody had checked. Silence is the
    honest answer when the answer is not known.
    """
    if is_local(base_url):
        return "local"
    p = preset_from_base_url(base_url)
    if not p:
        return ""
    if model in (p.get("free_models") or []):
        return "free"
    if model in (p.get("unsure_models") or []):
        return "unsure"
    return ""


def is_chat_model(name: str) -> bool:
    """Is this the kind of model you can hold a conversation with?

    A `/models` listing is everything the key can reach, which includes
    embedding, image and text-to-speech models that would fail on the first
    chat request. Excluded by what they are called, because that is all the
    listing gives -- crude, but wrong only in the direction of showing one
    extra name, never of hiding a usable model behind a guess.
    """
    n = (name or "").lower()
    return not any(w in n for w in (
        "embedding", "embed", "aqa", "imagen", "image-generation", "veo",
        "tts", "vision-only", "rerank", "moderation", "whisper", "learnlm"))


def preferred_model(available: list, base_url: str) -> str:
    """Which of these to start on.

    The catalogue's order is a preference, not a promise: whatever it names
    may be gone. So the first preference that is actually on offer wins, and
    failing that the first thing offered -- a working model nobody chose beats
    a chosen model that 404s.
    """
    have = [m for m in available if is_chat_model(m)]
    for want in chat_models(base_url):
        if want in have:
            return want
    return have[0] if have else ""


def chat_models(base_url: str) -> list:
    """The models of a known endpoint that are a chat choice.

    Not the same as every model it lists: z.ai's vision model is reached
    automatically for images and picking it to code with would be a mistake.
    Empty for an endpoint nobody knows -- there is nothing to enumerate.
    """
    p = preset_from_base_url(base_url)
    if not p:
        return []
    return list(p.get("chat_models") or p.get("models") or [])


def supports(base_url: str, extension: str) -> bool:
    """Does this endpoint understand a given non-standard request field?

    Unknown endpoints get False, and that asymmetry is the point. Sending a
    field a server does not know is not a soft failure: a strict validator --
    Google's is one -- rejects the entire request, so every turn fails with a
    message about a field the user never asked for. Omitting an extension only
    costs the feature it enables. Silence is the safe default here for the same
    reason it is with prices: this file cannot know what someone typed in.
    """
    p = preset_from_base_url(base_url)
    return bool(p and extension in (p.get("extensions") or []))


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
        # Whether to ask for a key at all, and whether the models have to be
        # read off a running server instead of listed here. Both default to
        # the hosted-provider answer, so adding an ordinary preset needs
        # neither field.
        c["needs_key"] = p.get("needs_key", True)
        c["local"] = is_local(p["base_url"])
        c["suggest_pull"] = p.get("suggest_pull", "")
        # Per model, not just per provider: a provider is not simply free or
        # paid. Three states rather than two, because "I do not know" is the
        # honest answer for some of them and pretending otherwise is how a
        # confident label in a config file turns into a wrong one six weeks
        # later. Anything unmarked is left unlabelled rather than assumed free.
        free = set(p.get("free_models") or [])
        unsure = set(p.get("unsure_models") or [])
        # Chat choices, not every model: the setup screen is where someone
        # decides what to code with, and z.ai's vision model is not that.
        c["model_options"] = [
            {"name": m,
             "tier": "free" if m in free else ("unsure" if m in unsure else "")}
            for m in (p.get("chat_models") or p["models"])]
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
        "needs_key": True,
        "local": False,
        "suggest_pull": "",
        "steps": [
            "Paste the API's base URL (the part ending in /v1 or similar).",
            "Paste a key if it needs one — local servers usually do not.",
            "Type the model name exactly as the provider spells it.",
        ],
    })
    return out
