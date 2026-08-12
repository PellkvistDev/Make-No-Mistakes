"""Speech to speech over the Gemini Live API.

Voice mode is four hops today: the page records you, faster-whisper turns that
into text, a text model answers, and Kokoro reads the answer back. Each hop
adds its own latency and its own failure, and two of them exist only to get
sound in and out of a model that cannot hear.

`gemini-3.1-flash-live-preview` hears and speaks natively over one WebSocket,
which removes both. It also removes the parts of voice mode this app had to
build by hand: barge-in, endpointing, and the noise-floor calibration behind
them are the server's job here, and the model can hear that you interrupted
rather than inferring it from a volume threshold.

WHAT LIVES WHERE, and why it is not all in Python:

The socket is opened by the PAGE, not by this process. The mic and the
speakers are already there, and pywebview's JS bridge is the app's known
bottleneck -- WebEvents batches its traffic for exactly that reason. Pushing
16kHz PCM through it in both directions would be the worst possible use of it.

So Python's job is the two things the page must not do: decide what the
session is (model, prompt, tools, voice), and RUN the tools. This module is
the first half and is deliberately pure -- it builds messages and never opens
anything, so all of it is testable without a network.

THE PROTOCOL, in the parts that matter here:

  - one setup message, then bidirectional frames
  - audio in:  raw PCM, 16-bit little-endian, mono, 16kHz
  - audio out: raw PCM, 16-bit little-endian, mono, 24kHz
  - a session may return AUDIO or TEXT, never both, so the spoken words come
    back as a separate transcription rather than as a second modality
  - function calling is SYNCHRONOUS ONLY: the model stops and waits for the
    result. That would be fatal for a conversation, except that the tools this
    agent has are already built to return instantly -- dispatch_worker exists
    precisely so the assistant never goes quiet while work happens. The
    constraint and the design happen to agree.
  - a session runs ~15 minutes and a single socket ~10, so both ends have to
    treat a reconnect as normal rather than as an error.
"""

from __future__ import annotations

import copy

from . import providers as _providers

# The endpoint is versioned separately from the REST surface and takes the key
# as a query parameter -- there is no Authorization header on a browser
# WebSocket, which is the whole reason this shape exists.
WS_HOST = "generativelanguage.googleapis.com"
WS_PATH = ("/ws/google.ai.generativelanguage.v1beta.GenerativeService"
           ".BidiGenerateContent")

# Fixed by the API, not preferences. Named because the page has to resample to
# the first and schedule playback at the second, and two different numbers
# silently produce a chipmunk.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
INPUT_MIME = f"audio/pcm;rate={INPUT_SAMPLE_RATE}"

# Everything the JSON Schema subset Gemini accepts does NOT include. Passing
# one through is not ignored: the setup is rejected and the session never
# opens, which looks exactly like a bad key.
_SCHEMA_KEYS = ("type", "description", "enum", "items", "properties",
                "required", "nullable", "format")


def ws_url(api_key: str) -> str:
    return f"wss://{WS_HOST}{WS_PATH}?key={api_key}"


def available(base_url: str) -> str:
    """The live model this endpoint serves, or "" if it serves none."""
    return _providers.live_model_for(base_url)


def _clean_schema(node):
    """A parameter schema with only the keys Gemini understands.

    Recursive because the trimming has to reach nested object properties and
    array items, and a stray key three levels down fails the setup exactly as
    loudly as one at the top.
    """
    if not isinstance(node, dict):
        return node
    out = {}
    for k in _SCHEMA_KEYS:
        if k not in node:
            continue
        v = node[k]
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _clean_schema(sub) for name, sub in v.items()}
        elif k == "items":
            out[k] = _clean_schema(v)
        else:
            out[k] = copy.deepcopy(v)
    # An object with no properties at all is rejected; a function that takes no
    # arguments is a real thing, so it is declared without a parameter block.
    if out.get("type") == "object" and not out.get("properties"):
        return {}
    return out


def function_declarations(schemas: list) -> list:
    """OpenAI-shaped tool schemas -> Gemini function declarations.

    The app declares its tools once, in OpenAI's shape, because every provider
    it talks to over /chat/completions takes that shape. The Live API is the
    one endpoint that does not, so the translation lives here rather than
    forcing a second copy of every schema to be maintained alongside the first.
    """
    out = []
    for s in schemas or []:
        fn = s.get("function") if s.get("type") == "function" else s
        if not fn or not fn.get("name"):
            continue
        decl = {"name": fn["name"], "description": fn.get("description", "")}
        params = _clean_schema(fn.get("parameters") or {})
        if params:
            decl["parameters"] = params
        out.append(decl)
    return out


def setup_message(model: str, system_prompt: str, schemas: list,
                  voice: str = "", language: str = "",
                  resume_handle: str = "") -> dict:
    """The one message that opens a session.

    `resume_handle` is what makes a dropped socket a non-event. Sessions
    outlive connections here -- ~15 minutes against ~10 -- so every long
    conversation reconnects at least once by design, and without this each
    reconnect would start a stranger who has never met you.
    """
    cfg: dict = {"responseModalities": ["AUDIO"]}
    if voice:
        cfg["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}},
        }
    if language:
        # Only when asked. Left unset the model answers in whatever it was
        # spoken to in, which is what "match what I speak" means -- and it does
        # that natively here, where the old pipeline needed Whisper's language
        # guess and a TTS voice that could pronounce the result.
        cfg.setdefault("speechConfig", {})["languageCode"] = language
    setup: dict = {
        "model": f"models/{model}",
        "generationConfig": cfg,
        # SIBLINGS of generationConfig, not fields inside it. BidiGenerateContentSetup
        # keeps only the generation parameters -- modalities, speech, sampling --
        # under generationConfig; transcription, tools, system instruction,
        # resumption and compression are all top-level. Nested, they are unknown
        # fields, and the server does not ignore an unknown field: it rejects the
        # setup and closes the socket. From the app that looked like the
        # connection dropping, five times, which is what it reported.
        #
        # Asked for explicitly because a session returns AUDIO or TEXT and never
        # both -- without these the words are never available as text at all, and
        # every voice turn goes into the chat's searchable transcript.
        "outputAudioTranscription": {},
        "inputAudioTranscription": {},
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        # Extends a session past its token ceiling by summarising the oldest
        # turns instead of ending the call mid-sentence at fifteen minutes.
        "contextWindowCompression": {"slidingWindow": {}},
        "sessionResumption": ({"handle": resume_handle} if resume_handle
                              else {}),
    }
    decls = function_declarations(schemas)
    if decls:
        setup["tools"] = [{"functionDeclarations": decls}]
    return {"setup": setup}


def tool_response(responses: list) -> dict:
    """Results going back for a batch of function calls.

    A batch, not one: the model may ask for several at once, and it is blocked
    on all of them until they are answered together. Each entry keeps the id it
    arrived with -- the pairing is the whole content of the message.
    """
    return {"toolResponse": {"functionResponses": [
        {"id": r.get("id", ""), "name": r.get("name", ""),
         "response": {"output": r.get("output", "")}}
        for r in responses or []]}}


def text_turn(text: str) -> dict:
    """Text injected into a live session as though it had been spoken.

    This is how a background worker finishing reaches the conversation. Sent as
    realtime input rather than as client content, because client content is for
    seeding history before the session starts and using it mid-call puts the
    message in the wrong place in the model's view of the conversation.
    """
    return {"realtimeInput": {"text": text}}


def audio_chunk(b64_pcm: str) -> dict:
    return {"realtimeInput": {"audio": {"data": b64_pcm, "mimeType": INPUT_MIME}}}


def audio_stream_end() -> dict:
    """Sent when the mic stops.

    Without it the server holds the tail of what was said in its buffer waiting
    for more, so muting mid-sentence loses the sentence.
    """
    return {"realtimeInput": {"audioStreamEnd": True}}
