"""Retry/backoff behavior of ZaiClient.chat and RateLimiter spacing."""

import threading
import time

import pytest

import glmcode.api as api
from glmcode.api import ApiError, RateLimiter, ZaiClient


class _Client(ZaiClient):
    """ZaiClient with _stream_once scripted; no network ever."""

    def __init__(self, script):
        self.rate_limiter = None
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
