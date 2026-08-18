"""Building a Gemini Live session, and running the tools it asks for.

Everything here is checked without a socket. The module that builds the
session is deliberately pure -- it produces messages and never opens anything
-- because the alternative is a feature whose only test is "plug in a
microphone and listen", which is how the iOS keyboard work in this repo ended
up costing a round trip to a real phone per guess.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import types

import pytest

from glmcode import live, providers
from glmcode.config import Config
from glmcode.errors import ToolError
from glmcode.tools import CONVERSATIONAL_SCHEMAS

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))
from glmcode.gui import app as gui_app  # noqa: E402

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"
ZAI = "https://api.z.ai/api/paas/v4"


# ---- which endpoints can do this at all ---------------------------------- #

def test_only_an_endpoint_that_implements_the_protocol_offers_it():
    """Not a capability that degrades. The Live API is a WebSocket carrying raw
    PCM; an endpoint either speaks it or there is nothing to fall back to."""
    assert live.available(GOOGLE) == "gemini-3.1-flash-live-preview"
    assert live.available(ZAI) == ""
    assert live.available("https://something-nobody-knows.test/v1") == ""


def test_the_live_model_is_not_offered_as_something_to_chat_with():
    """It is not reachable over /chat/completions. Listing it among the models
    would put it in the model picker, where choosing it produces a chat that
    fails on its first message."""
    p = providers.preset("google")
    assert p["live_model"] not in p["models"]
    assert p["live_model"] not in providers.chat_models(GOOGLE)


def test_the_key_rides_in_the_url_because_a_browser_socket_has_no_headers():
    assert live.ws_url("sk-abc").startswith("wss://generativelanguage.googleapis.com/ws/")
    assert live.ws_url("sk-abc").endswith("?key=sk-abc")


# ---- the setup message --------------------------------------------------- #

def _setup(**kw):
    return live.setup_message("m", "be helpful", CONVERSATIONAL_SCHEMAS,
                              **kw)["setup"]


def _speech(**kw):
    # Under generationConfig, not beside it -- voice and language are
    # generation settings for this API, and a speechConfig at the top level is
    # silently ignored rather than rejected.
    return _setup(**kw)["generationConfig"].get("speechConfig", {})


def test_a_session_asks_for_audio_and_for_the_words_that_went_with_it():
    """A session returns AUDIO or TEXT, never both -- so without asking for
    transcription there is no text at all, and this app writes every voice turn
    into the chat's searchable transcript."""
    s = _setup()
    assert s["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert "outputAudioTranscription" in s
    assert "inputAudioTranscription" in s


def test_transcription_is_a_sibling_of_generationconfig_not_a_field_in_it():
    """The reported failure. BidiGenerateContentSetup keeps only generation
    parameters under generationConfig; transcription is top-level. Nested,
    these are unknown fields -- and the server does not ignore an unknown
    field, it rejects the setup and closes the socket. The app read that as
    the connection dropping and said so, five times."""
    gen = _setup()["generationConfig"]
    assert "outputAudioTranscription" not in gen
    assert "inputAudioTranscription" not in gen
    # Only generation parameters live in there.
    assert set(gen) <= {"responseModalities", "speechConfig", "temperature",
                        "topP", "topK", "maxOutputTokens", "mediaResolution"}


def test_every_top_level_key_is_one_the_setup_message_defines():
    """A single unrecognised key anywhere here costs the whole session, and the
    symptom names none of them -- so the set is pinned rather than trusted."""
    allowed = {"model", "generationConfig", "systemInstruction", "tools",
               "realtimeInputConfig", "sessionResumption",
               "contextWindowCompression", "inputAudioTranscription",
               "outputAudioTranscription", "proactivity"}
    assert set(_setup()) <= allowed


def test_a_session_can_outlive_its_connection():
    """Sessions run ~15 minutes and a socket ~10, so every long conversation
    reconnects at least once. Without these two it would end mid-sentence, or
    come back as a stranger."""
    s = _setup()
    assert "contextWindowCompression" in s
    assert "sessionResumption" in s
    assert live.setup_message("m", "p", [], resume_handle="h-1")[
        "setup"]["sessionResumption"] == {"handle": "h-1"}


def test_the_language_is_left_unset_when_you_want_it_to_follow_you():
    """"Match what I speak" is native here. The old pipeline needed Whisper's
    language guess and a TTS voice that could pronounce the answer."""
    assert "languageCode" not in _speech()
    assert _speech(language="en-US")["languageCode"] == "en-US"


def test_the_voice_is_passed_through():
    sc = _speech(voice="Puck")
    assert sc["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"


# ---- tool schemas -------------------------------------------------------- #

def test_the_apps_tools_are_translated_rather_than_duplicated():
    """They are declared once in OpenAI's shape because every other endpoint
    this app talks to takes that shape. The Live API is the one that does not."""
    decls = live.function_declarations(CONVERSATIONAL_SCHEMAS)
    names = [d["name"] for d in decls]
    assert "dispatch_worker" in names
    d = next(d for d in decls if d["name"] == "dispatch_worker")
    assert d["parameters"]["type"] == "object"
    assert set(d["parameters"]["properties"]) == {"name", "task"}
    assert d["parameters"]["required"] == ["task"]
    assert d["description"]
    # Flattened: no OpenAI-style {"type": "function", "function": {...}} left.
    assert "function" not in d and "type" not in d


def test_a_key_gemini_does_not_know_fails_the_whole_session():
    """Not ignored -- the setup is rejected and no session opens, which from
    the outside looks exactly like a bad API key."""
    schemas = [{"type": "function", "function": {
        "name": "t", "description": "d",
        "parameters": {"type": "object", "additionalProperties": False,
                       "$schema": "http://json-schema.org/draft-07/schema#",
                       "properties": {"a": {"type": "string", "title": "A"}},
                       "required": ["a"]}}}]
    p = live.function_declarations(schemas)[0]["parameters"]
    assert set(p) == {"type", "properties", "required"}
    assert set(p["properties"]["a"]) == {"type"}


def test_the_trim_reaches_all_the_way_down():
    """A stray key three levels in fails the setup exactly as loudly as one at
    the top, so the trim cannot stop at the first level."""
    schemas = [{"type": "function", "function": {
        "name": "t", "description": "d",
        "parameters": {"type": "object", "properties": {
            "items": {"type": "array", "minItems": 1,
                      "items": {"type": "object", "additionalProperties": True,
                                "properties": {"x": {"type": "string",
                                                     "default": "no"}}}}}}}}]
    arr = live.function_declarations(schemas)[0]["parameters"]["properties"]["items"]
    assert set(arr) == {"type", "items"}
    assert set(arr["items"]["properties"]["x"]) == {"type"}


def test_a_tool_that_takes_no_arguments_declares_no_parameters():
    """An object schema with no properties is rejected outright, and a function
    with no arguments is an ordinary thing to want."""
    schemas = [{"type": "function", "function": {
        "name": "check_workers", "description": "d",
        "parameters": {"type": "object", "properties": {}, "required": []}}}]
    assert "parameters" not in live.function_declarations(schemas)[0]


# ---- the frames ---------------------------------------------------------- #

def test_results_go_back_as_a_batch_keyed_by_call_id():
    """The model may ask for several at once and is blocked on all of them.
    The pairing is the entire content of the message."""
    msg = live.tool_response([
        {"id": "c1", "name": "dispatch_worker", "output": "started"},
        {"id": "c2", "name": "check_workers", "output": "none"},
    ])["toolResponse"]["functionResponses"]
    assert [r["id"] for r in msg] == ["c1", "c2"]
    assert msg[0]["response"] == {"output": "started"}


def test_a_note_from_the_app_is_sent_as_realtime_input():
    """How a finished background worker reaches the conversation. Client
    content is for seeding history before the session starts; using it
    mid-call puts the message in the wrong place in the model's view."""
    assert live.text_turn("worker done")["realtimeInput"]["text"] == "worker done"


def test_the_microphone_stopping_is_itself_a_message():
    """Without it the server holds the tail of the sentence waiting for more,
    so muting mid-sentence loses the sentence."""
    assert live.audio_stream_end()["realtimeInput"]["audioStreamEnd"] is True


def test_audio_is_labelled_with_the_rate_it_was_resampled_to():
    a = live.audio_chunk("AAAA")["realtimeInput"]["audio"]
    assert a["data"] == "AAAA"
    assert a["mimeType"] == "audio/pcm;rate=16000"
    assert live.INPUT_SAMPLE_RATE == 16000
    # Different in each direction, and mixing them up is a chipmunk.
    assert live.OUTPUT_SAMPLE_RATE == 24000


# ---- the bridge ---------------------------------------------------------- #

class _Convo:
    def __init__(self, out="ok", boom=None):
        self.out, self.boom, self.calls = out, boom, []
        self.messages = []

    def system_prompt_text(self):
        return "you are a delegator"

    def _run_tool(self, name, args, assistant_idx=-1):
        self.calls.append((name, args))
        if self.boom:
            raise self.boom
        return self.out


def _api(cfg, convo=None):
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = cfg
    api._client = None
    cs = types.SimpleNamespace(sid="s1", agent=None, convo_agent=convo,
                               convo_events=None, provider="", model="")
    api._chats = {"s1": cs}
    api.session_id = "s1"
    return api, cs


def test_the_page_is_told_where_to_connect_and_what_to_send(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-live")
    cfg = Config(provider_preset="google", base_url=GOOGLE,
                 model="gemini-3.6-flash")
    convo = _Convo()
    api, _ = _api(cfg, convo)
    monkeypatch.setattr(api, "_ensure_convo", lambda cs: convo, raising=False)

    res = api.live_voice_config()
    assert res["ok"] is True
    assert res["url"].endswith("?key=sk-live")
    assert res["setup"]["setup"]["model"] == "models/gemini-3.1-flash-live-preview"
    assert res["setup"]["setup"]["systemInstruction"]["parts"][0]["text"] \
        == "you are a delegator"
    assert (res["inputRate"], res["outputRate"]) == (16000, 24000)


def test_an_api_that_cannot_do_it_says_which_one_and_what_to_do(monkeypatch):
    """Which API a chat is on is a per-chat choice, so "unsupported" is not
    enough to act on."""
    cfg = Config(provider_preset="zai", base_url=ZAI, api_key="k")
    api, _ = _api(cfg, _Convo())
    err = api.live_voice_config()["error"]
    assert "Z.AI" in err and "Google AI Studio" in err


def test_a_tool_call_reaches_the_conversational_agent(monkeypatch):
    convo = _Convo(out="worker w1 started")
    api, _ = _api(Config(), convo)
    res = api.live_voice_tool("dispatch_worker", {"task": "fix the build"})
    assert res["output"] == "worker w1 started"
    assert convo.calls == [("dispatch_worker", {"task": "fix the build"})]


@pytest.mark.parametrize("boom, expect", [
    (ToolError("no such worker"), "ERROR: no such worker"),
    (RuntimeError("kaboom"), "ERROR: unexpected RuntimeError: kaboom"),
])
def test_a_failing_tool_is_something_to_talk_about_not_a_dropped_call(boom, expect):
    """The model is stopped waiting for this answer. Raising here would hang
    the conversation instead of letting it say what went wrong."""
    api, _ = _api(Config(), _Convo(boom=boom))
    assert api.live_voice_tool("dispatch_worker", {"task": "x"})["output"] == expect


def test_a_spoken_exchange_is_written_back_into_the_conversation():
    """The model keeps the conversation on its side, so nothing lands here by
    itself -- and switching back to the local engine mid-chat has to continue
    rather than restart."""
    convo = _Convo()
    api, cs = _api(Config(), convo)
    persisted = []
    # reply= is passed now: the live engine HAS Gemini's transcript of both
    # halves and hands them over, rather than having the reply dug back out
    # of the delegator's history.
    api._persist_voice_turn = lambda c, t, reply=None: persisted.append(t)

    api.live_voice_turn("what's failing?", "the login test")
    assert convo.messages == [
        {"role": "user", "content": "what's failing?"},
        {"role": "assistant", "content": "the login test"},
    ]
    assert persisted == ["what's failing?"]


# --------------------------------------------------------------------- #
# The phone builds the same session.
#
# Both devices open their own socket to the same model with the same tools, so
# a difference here is not cosmetic: it is one device offering the model a tool
# the other does not, or asking for transcription the other omits. The same
# rule already holds for heal_interrupted_turn, and for the same reason -- a
# chat moves between these two.

_CORE_JS = pathlib.Path(__file__).resolve().parent.parent / "mobile" / "agent-core.js"
needs_node = pytest.mark.skipif(
    not (shutil.which("node") and _CORE_JS.is_file()),
    reason="node or mobile/agent-core.js unavailable")


def _phone(expr, *args):
    out = subprocess.run(
        ["node", "-e",
         "const C=require(process.argv[1]);"
         "const a=JSON.parse(process.argv[2]);"
         f"console.log(JSON.stringify({expr}));",
         str(_CORE_JS), json.dumps(list(args))],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_the_phone_and_the_desktop_open_the_same_session():
    desktop = live.setup_message("m", "be helpful", CONVERSATIONAL_SCHEMAS,
                                 voice="Puck", language="en-US")
    phone = _phone("C.liveSetup(a[0], a[1], a[2], {voice: a[3], language: a[4]})",
                   "m", "be helpful", CONVERSATIONAL_SCHEMAS, "Puck", "en-US")
    assert phone == desktop


@needs_node
def test_the_two_agree_about_resuming_a_session():
    assert _phone("C.liveSetup('m', 'p', [], {resumeHandle: a[0]})", "h-1") \
        == live.setup_message("m", "p", [], resume_handle="h-1")


@needs_node
def test_the_two_trim_tool_schemas_identically():
    """The trim is the part with the most room to drift, and the failure it
    causes is the least obvious: a session that never opens."""
    schemas = [{"type": "function", "function": {
        "name": "t", "description": "d",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {
                           "a": {"type": "string", "title": "A"},
                           "rows": {"type": "array", "minItems": 1,
                                    "items": {"type": "object", "properties": {
                                        "x": {"type": "string", "default": "no"}}}}},
                       "required": ["a"]}}}]
    assert _phone("C.liveFunctionDeclarations(a[0])", schemas) \
        == live.function_declarations(schemas)


@needs_node
def test_the_two_send_the_same_frames():
    assert _phone("C.liveToolResponse(a[0])",
                  [{"id": "c1", "name": "n", "output": "o"}]) \
        == live.tool_response([{"id": "c1", "name": "n", "output": "o"}])
    assert _phone("C.liveTextTurn(a[0])", "hi") == live.text_turn("hi")
    assert _phone("C.liveAudioChunk(a[0])", "AAAA") == live.audio_chunk("AAAA")
    assert _phone("C.liveAudioStreamEnd()") == live.audio_stream_end()


@needs_node
def test_the_two_agree_on_the_model_and_the_two_rates():
    got = _phone("[C.LIVE_MODEL, C.LIVE_INPUT_RATE, C.LIVE_OUTPUT_RATE, C.liveWsUrl('k')]")
    assert got[0] == providers.preset("google")["live_model"]
    assert got[1:3] == [live.INPUT_SAMPLE_RATE, live.OUTPUT_SAMPLE_RATE]
    assert got[3] == live.ws_url("k")


@needs_node
def test_the_phone_resamples_and_clips_the_same_way():
    """Checked here rather than only in the browser: it is arithmetic, it is
    wrong silently, and a phone mic commonly runs at 48k."""
    assert _phone("C.livePcm16(new Float32Array(48000), 48000).byteLength") == 32000
    assert _phone("[...new Int16Array(C.livePcm16(new Float32Array([1,-1,0]), 16000).buffer)]") \
        == [32767, -32768, 0]


@needs_node
def test_the_phone_speaks_rather_than_writes():
    """The coding prompt asks for paths, code blocks and diffs. None of that
    survives being read out loud on a phone."""
    p = _phone("C.LIVE_VOICE_PROMPT")
    assert "out loud" in p
    assert "needs_desktop" in p, "the phone has no shell; that has to be said"
    for written in ("markdown", "code block"):
        assert written in p.lower()
