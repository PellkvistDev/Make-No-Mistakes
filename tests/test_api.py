"""Retry/backoff behavior of ZaiClient.chat and RateLimiter spacing."""

import json
import threading
import types
import time

import pytest

import glmcode.api as api
from glmcode.api import ApiError, RateLimiter, ZaiClient


class _Client(ZaiClient):
    """ZaiClient with _stream_once scripted; no network ever."""

    def __init__(self, script):
        # Through the real __init__: the client derives things from base_url
        # now (which vendor extensions the endpoint understands), and a double
        # that skips construction stops resembling the thing it stands in for.
        super().__init__("k", "https://api.z.ai/api/paas/v4")
        self.n = 0
        self._script = script

    def _stream_once(self, *a, **k):
        self.n += 1
        return self._script(self.n)


@pytest.fixture
def recorded_sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(api.time, "sleep", lambda s: waits.append(s))
    return waits


def test_transient_429_recovers_with_jittered_backoff(recorded_sleeps):
    def script(n):
        if n < 4:
            raise ApiError(429, "rate limited")
        return "OK"

    c = _Client(script)
    assert c.chat(model="m", messages=[]) == "OK"
    assert c.n == 4  # 3 failures, success on the 4th
    # Equal jitter: wait_i in [base/2, base], base = min(2**attempt, 30),
    # with a floor of 2 for 429s.
    lo_hi = [(1, 2), (2, 4), (4, 8)]
    assert len(recorded_sleeps) == 3
    for wait, (lo, hi) in zip(recorded_sleeps, lo_hi):
        assert lo <= wait <= hi, (wait, lo, hi)


def test_backoff_is_jittered_not_fixed():
    # The whole point of jitter: parallel clients must NOT all pick the same
    # wait. Collect attempt-1 waits from many fresh runs and require spread.
    import glmcode.api as api_mod
    waits = set()
    real_sleep = api_mod.time.sleep
    api_mod.time.sleep = lambda s: waits.add(s)
    try:
        for _ in range(60):
            calls = {"n": 0}

            def script(n, calls=calls):
                if n == 1:
                    raise ApiError(429, "rl")
                return "OK"

            _Client(script).chat(model="m", messages=[])
    finally:
        api_mod.time.sleep = real_sleep
    assert len(waits) > 3, f"backoff looks fixed, not jittered: {waits}"


def test_gives_up_after_max_retries(recorded_sleeps):
    def script(n):
        raise ApiError(429, "always down")

    c = _Client(script)
    with pytest.raises(ApiError) as ei:
        c.chat(model="m", messages=[])
    assert c.n == api.MAX_RETRIES
    assert "gave up" in str(ei.value)


def test_non_retryable_fails_immediately(recorded_sleeps):
    def script(n):
        raise ApiError(400, "bad request")

    c = _Client(script)
    with pytest.raises(ApiError):
        c.chat(model="m", messages=[])
    assert c.n == 1
    assert recorded_sleeps == []


def test_rate_limiter_spaces_out_concurrent_threads():
    # 0.2s, not 0.05s. Windows' clock ticks roughly every 15.6ms, so at 50ms
    # spacing a perfectly correct limiter can *measure* gaps as short as ~31ms
    # and fail a tight per-gap assertion -- which it did in CI, with every
    # observed gap landing on a multiple of the tick. Measuring at 4x the
    # granularity turns that quantisation back into noise instead of signal.
    interval = 0.2
    limiter = RateLimiter(min_interval=interval)
    stamps = []
    lock = threading.Lock()

    def worker():
        limiter.wait()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # The property that actually matters: four calls cannot be squeezed into
    # less than three intervals' worth of wall clock. Totals are the robust
    # measurement here -- per-gap quantisation errors average out across the
    # span rather than compounding.
    assert stamps[-1] - stamps[0] >= interval * 3 * 0.8, stamps
    # And no two were allowed to bunch up, which a limiter that slept once and
    # then released everything at once would show.
    assert all(g >= interval * 0.6 for g in gaps), gaps


# ---------------------------------------------------------- token counting
# Dividing characters by a constant is a guess, and that guess is why the
# context limit had to sit well under the model's real window. The API reports
# the exact prompt_tokens for every request it answers, so measure instead.

def test_estimate_tokens_uses_the_ratio_it_is_given():
    msgs = [{"role": "user", "content": "a" * 3600}]
    assert api.estimate_tokens(msgs) == 1000                # default 3.6
    assert api.estimate_tokens(msgs, 2.0) == 1800           # denser tokenisation
    assert api.estimate_tokens(msgs, 0) == 1000, "a nonsense ratio falls back"


def test_message_chars_counts_images_at_a_flat_cost():
    huge = "data:image/png;base64," + "A" * 200_000
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": huge}}]}]
    # Counting the data URL by length would read as tens of thousands of tokens.
    assert api.message_chars(msgs) == 4000


def test_calibrate_ratio_measures_from_a_priced_request():
    msgs = [{"role": "user", "content": "a" * 1000}]
    assert api.calibrate_ratio(msgs, 250) == 4.0
    assert api.calibrate_ratio(msgs, 500) == 2.0


def test_calibrate_ratio_rejects_impossible_readings():
    """One odd response must not be able to skew the meter."""
    msgs = [{"role": "user", "content": "a" * 1000}]
    assert api.calibrate_ratio(msgs, 0) is None
    assert api.calibrate_ratio(msgs, -1) is None
    assert api.calibrate_ratio([], 100) is None            # nothing to measure
    assert api.calibrate_ratio(msgs, 1) is None            # 1000 chars/token
    assert api.calibrate_ratio(msgs, 900) is None          # ~1 char/token


# --------------------------------------------------------------------- #
# Vendor extensions.
#
# `thinking` is a Zhipu field, not an OpenAI one. It was sent to every
# endpoint, because when it was added there was only one endpoint. Google's
# compatibility layer validates strictly and rejects the WHOLE request:
#
#   400 Invalid JSON payload received. Unknown name "thinking":
#       Cannot find field.
#
# so every turn failed, over a field the user never asked for.

class _Capture(ZaiClient):
    """A real client, but the request is captured instead of being sent."""

    def __init__(self, base_url):
        super().__init__("k", base_url)
        self.payloads = []

    def _stream_once(self, payload, *a, **k):
        self.payloads.append(payload)
        from glmcode.api import ChatResult
        return ChatResult(content="ok")


def _sent(base_url, **kw):
    c = _Capture(base_url)
    c.chat(model="m", messages=[{"role": "user", "content": "hi"}], **kw)
    return c.payloads[0]


def test_thinking_is_not_sent_to_google():
    payload = _sent("https://generativelanguage.googleapis.com/v1beta/openai",
                    thinking=True)
    assert "thinking" not in payload


def test_thinking_is_still_sent_to_zai():
    """Turning it off everywhere would be the other way to break this."""
    payload = _sent("https://api.z.ai/api/paas/v4", thinking=True)
    assert payload["thinking"] == {"type": "enabled"}


def test_thinking_is_not_sent_to_an_endpoint_nobody_knows():
    """A hand-typed URL could be anything. Omitting an extension costs the
    feature; sending one a strict validator rejects costs every turn."""
    for url in ("http://localhost:11434/v1", "https://openrouter.ai/api/v1"):
        assert "thinking" not in _sent(url, thinking=True), url


def test_asking_for_no_thinking_still_means_no_thinking():
    assert "thinking" not in _sent("https://api.z.ai/api/paas/v4", thinking=False)


def test_nothing_else_in_the_payload_is_vendor_specific():
    """The guard against a sixth round of this. Every other field sent must be
    one the OpenAI chat-completions schema defines, or a strict endpoint will
    reject the request the same way."""
    openai_fields = {"model", "messages", "temperature", "max_tokens",
                     "stream", "tools", "tool_choice"}
    payload = _sent("https://generativelanguage.googleapis.com/v1beta/openai",
                    tools=[{"type": "function",
                            "function": {"name": "f", "parameters": {}}}])
    assert set(payload) <= openai_fields, set(payload) - openai_fields


# --------------------------------------------------------------------- #
# Thought signatures.
#
# Gemini 3 attaches an encrypted record of the reasoning behind each tool call
# and REQUIRES it back on every following request:
#
#   400 Function call is missing a thought_signature in functionCall parts.
#       ... function call `default_api:list_dir`, position 4
#
# "Position 4" is the shape of it: the first tool call is fine and the turn
# dies a step or two later, once history has to carry the signature forward.
# Rebuilding tool calls from id/name/arguments dropped it.

GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai"
SIG = {"google": {"thought_signature": "Ct8BAdHtim8..."}}


class _FakeResp:
    """Just enough of requests' streaming response for the real parser."""

    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


def _sse(*deltas):
    """SSE lines carrying tool_call deltas, as Google actually sends them."""
    out = []
    for d in deltas:
        out.append("data: " + json.dumps(
            {"choices": [{"delta": {"tool_calls": d}}]}))
    out.append("data: [DONE]")
    return out


class _Stream(ZaiClient):
    """A real client reading a scripted SSE body.

    Deliberately NOT a reimplementation of the accumulator: an earlier version
    of this double rebuilt tool calls itself, so deleting the real code under
    test changed nothing and the test passed against the bug it was written
    for. Only the socket is faked.
    """

    def __init__(self, base_url, lines):
        super().__init__("k", base_url)
        self.session = types.SimpleNamespace(
            headers={}, post=lambda *a, **k: _FakeResp(lines))


def test_a_thought_signature_survives_the_tool_call_being_rebuilt():
    """It arrives on one delta and the arguments arrive on others; the call is
    reassembled from all of them and the signature has to come along."""
    c = _Stream(GOOGLE, _sse(
        [{"index": 0, "id": "c1", "function": {"name": "list_dir"},
          "extra_content": SIG}],
        [{"index": 0, "function": {"arguments": '{"path"'}}],
        [{"index": 0, "function": {"arguments": ': "."}'}}],
    ))
    res = c.chat(model="m", messages=[{"role": "user", "content": "hi"}])

    tc = res.tool_calls[0]
    assert tc["function"]["arguments"] == '{"path": "."}'
    assert tc["extra_content"] == SIG


def test_the_signature_is_sent_back_to_google():
    """The whole point: it must reach the NEXT request or that request 400s."""
    c = _Capture(GOOGLE)
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "extra_content": SIG,
             "function": {"name": "list_dir", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
    ]
    c.chat(model="m", messages=history)
    sent = c.payloads[0]["messages"][1]["tool_calls"][0]
    assert sent["extra_content"] == SIG


def test_the_signature_is_not_forwarded_to_a_provider_that_never_issued_it():
    """A chat can change provider mid-conversation. Sending Google's field on
    to z.ai is the same mistake as sending z.ai's `thinking` to Google."""
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "extra_content": SIG,
             "function": {"name": "list_dir", "arguments": "{}"}}]},
    ]
    c = _Capture("https://api.z.ai/api/paas/v4")
    c.chat(model="m", messages=history)

    sent = c.payloads[0]["messages"][0]["tool_calls"][0]
    assert "extra_content" not in sent
    assert sent["function"]["name"] == "list_dir"   # the call itself intact


def test_trimming_for_one_provider_does_not_damage_the_stored_history():
    """Switch to z.ai and back, and Google's signatures must still be there --
    so the trim happens on the copy that goes on the wire, not in place."""
    history = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "extra_content": SIG,
             "function": {"name": "list_dir", "arguments": "{}"}}]},
    ]
    _Capture("https://api.z.ai/api/paas/v4").chat(
        model="m", messages=history)
    assert history[0]["tool_calls"][0]["extra_content"] == SIG


def test_a_history_with_nothing_to_trim_is_passed_straight_through():
    history = [{"role": "user", "content": "hi"}]
    c = _Capture("https://api.z.ai/api/paas/v4")
    c.chat(model="m", messages=history)
    assert c.payloads[0]["messages"] is history


# --------------------------------------------------------------------- #
# Stop, during a retry.
#
# A rate limit leaves the client asleep between attempts, and the only cancel
# check was inside the loop reading a response body -- which is not running
# then. So pressing Stop during a 429 backoff did nothing visible and the
# client carried on hammering the API afterwards.

class _AlwaysRateLimited(ZaiClient):
    def __init__(self, base_url="https://api.z.ai/api/paas/v4"):
        super().__init__("k", base_url)
        self.attempts = 0

    def _stream_once(self, payload, *a, **k):
        self.attempts += 1
        raise ApiError(429, "rate limited")


def test_stop_is_heard_during_a_retry_backoff():
    """Set the flag while it is asleep between attempts, as a person pressing
    Stop does -- it must give up then, not after the wait expires."""
    import threading as _t
    from glmcode.api import Cancelled

    c = _AlwaysRateLimited()
    cancel = _t.Event()
    _t.Timer(0.15, cancel.set).start()

    started = time.monotonic()
    with pytest.raises(Cancelled):
        c.chat(model="m", messages=[{"role": "user", "content": "hi"}],
               cancel=cancel)
    # Promptly, not "eventually": the first backoff is 1-2s, so anything at or
    # above that means the sleep was slept through and only noticed afterwards.
    assert time.monotonic() - started < 0.8
    assert c.attempts <= 2


def test_stop_before_the_first_request_sends_nothing():
    import threading as _t
    from glmcode.api import Cancelled

    c = _AlwaysRateLimited()
    cancel = _t.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        c.chat(model="m", messages=[{"role": "user", "content": "hi"}],
               cancel=cancel)
    assert c.attempts == 0


def test_without_a_cancel_the_retries_still_happen():
    """The stop path must not have quietly disabled retrying for everyone."""
    c = _AlwaysRateLimited()
    with pytest.raises(ApiError):
        c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert c.attempts > 1


# --------------------------------------------------------------------- #
# The server usually says how long to wait. This client ignored it.
#
#   429 ... Quota exceeded for metric: ..._input_token_count, limit: 16000
#           Please retry in 15.551629653s.
#
# answered with a 1.8s backoff -- so several attempts were spent re-asking a
# question the server had already answered, which is what a "retry storm" in
# the chat log actually is.

class _Resp429:
    def __init__(self, headers=None, msg="rate limited"):
        self.status_code = 429
        self.headers = headers or {}
        self._msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def json(self):
        return {"error": {"message": self._msg}}


def test_the_servers_own_retry_delay_is_read_from_the_message():
    from glmcode.api import _retry_hint
    assert _retry_hint(_Resp429(), "Please retry in 15.551629653s.") == pytest.approx(15.55, abs=0.01)


def test_the_retry_after_header_is_read_when_present():
    from glmcode.api import _retry_hint
    assert _retry_hint(_Resp429({"Retry-After": "30"}), "no hint") == 30.0


def test_an_http_date_header_falls_through_to_the_message():
    """Retry-After may be a date. Rather than parse it, fall back to the text,
    which is where the providers here actually put the number."""
    from glmcode.api import _retry_hint
    r = _Resp429({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _retry_hint(r, "please retry in 9s") == 9.0


def test_no_hint_is_not_zero():
    """None means the server did not say -- distinct from "wait no time"."""
    from glmcode.api import _retry_hint
    assert _retry_hint(_Resp429(), "just broken") is None


def test_the_backoff_waits_at_least_as_long_as_the_server_asked():
    """The point of the whole thing: a 1.8s guess against a 15.5s instruction
    burns attempts to be told the same thing again."""
    import threading as _t
    from glmcode.api import ApiError as _AE

    waited = []

    class _Client(ZaiClient):
        def __init__(self):
            super().__init__("k", "https://api.z.ai/api/paas/v4")
            self.n = 0

        def _sleep_unless_cancelled(self, seconds, cancel):
            waited.append(seconds)
            if len(waited) >= 2:
                raise _t.ThreadError("stop the test")

        def _stream_once(self, payload, *a, **k):
            self.n += 1
            raise _AE(429, "quota exceeded. Please retry in 15.5s.",
                      retry_after=15.5)

    c = _Client()
    with pytest.raises((_t.ThreadError, ApiError)):
        c.chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert waited and waited[0] >= 15.5, waited


def test_a_hint_longer_than_the_cap_is_bounded():
    """A provider must not be able to park a turn indefinitely."""
    import threading as _t
    from glmcode.api import ApiError as _AE, MAX_RETRY_WAIT

    waited = []

    class _Client(ZaiClient):
        def __init__(self):
            super().__init__("k", "https://api.z.ai/api/paas/v4")

        def _sleep_unless_cancelled(self, seconds, cancel):
            waited.append(seconds)
            raise _t.ThreadError("stop")

        def _stream_once(self, payload, *a, **k):
            raise _AE(429, "retry in 3600s", retry_after=3600.0)

    with pytest.raises((_t.ThreadError, ApiError)):
        _Client().chat(model="m", messages=[{"role": "user", "content": "hi"}])
    assert waited[0] <= MAX_RETRY_WAIT
