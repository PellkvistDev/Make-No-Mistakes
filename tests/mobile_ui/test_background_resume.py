"""What happens to a running turn when iOS takes the app away.

Reported: with the agent working and the app not in focus, the turn just stops
and fails.

It is not a bug in the loop. runAgent runs in this page and calls the model with
fetch from the tab, so suspending the app kills the request under it and the
turn ends on its error path. No web API changes that -- WebKit has never shipped
Background Sync or Background Fetch, and Web Push can deliver a notification but
cannot run anything.

What is fixable is the two things covered here: not letting the screen lock
itself while a turn is running, and making a suspension cost the turn rather
than the conversation.
"""


def hide(phone):
    """Send the app to the background, as iOS does on lock or app-switch."""
    phone.page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
      Object.defineProperty(document, 'visibilityState',
        { configurable: true, get: () => 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")


def unhide(phone):
    phone.page.evaluate("""() => {
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      Object.defineProperty(document, 'visibilityState',
        { configurable: true, get: () => 'visible' });
      document.dispatchEvent(new Event('visibilitychange'));
    }""")


def install_wake_lock_spy(phone):
    """Record wake lock activity, and provide the API where there is none.

    Headless Chromium does not grant screen wake locks, so the real object
    cannot be relied on to be there; and the point of the test is what the app
    asks for, not what the platform decides to give it.
    """
    phone.page.evaluate("""() => {
      window.__wake = { requested: 0, released: 0 };
      Object.defineProperty(navigator, 'wakeLock', {
        configurable: true,
        get: () => ({
          request: async () => {
            window.__wake.requested++;
            return { release: async () => { window.__wake.released++; } };
          },
        }),
      });
    }""")


def wake(phone):
    return phone.page.evaluate("() => window.__wake")


# The interrupted flag lives in a closure, and exporting it just to be watched
# would put a test hook in the app. Everything below reads the behaviour it is
# supposed to produce instead: the notice, and whether another request goes out.
RESUME_NOTE = "went into the background"


def resumed(phone):
    return phone.page.evaluate(
        "(t) => Array.from(document.querySelectorAll('.bubble.system'))"
        ".some(e => (e.textContent || '').includes(t))", RESUME_NOTE)


def requests_made(phone):
    return phone.page.evaluate("() => window.__sent.length")


# ------------------------------------------------------- keeping the screen on

def test_a_running_turn_holds_the_screen_awake(phone):
    """The ordinary way this goes wrong is not app-switching, it is putting the
    phone down and letting it lock itself."""
    phone.setup()
    install_wake_lock_spy(phone)
    phone.reply({"role": "assistant", "content": "done"})
    phone.send("do something")
    phone.wait_idle()
    assert wake(phone)["requested"] >= 1, "the screen was left free to lock mid-turn"
    assert phone.errors == []


def test_the_screen_is_let_go_once_the_turn_ends(phone):
    """Holding it past the end would flatten the battery for no reason."""
    phone.setup()
    install_wake_lock_spy(phone)
    phone.reply({"role": "assistant", "content": "done"})
    phone.send("do something")
    phone.wait_idle()
    w = wake(phone)
    assert w["released"] >= 1, f"the wake lock was never released: {w}"
    assert phone.errors == []


def test_a_missing_wake_lock_api_does_not_break_the_turn(phone):
    """Below iOS 16.4, or on a non-secure origin, there is no wakeLock at all.
    A turn must still run."""
    phone.setup()
    phone.page.evaluate(
        "() => Object.defineProperty(navigator, 'wakeLock',"
        " { configurable: true, get: () => undefined })")
    phone.reply({"role": "assistant", "content": "still fine"})
    phone.send("do something")
    phone.wait_idle()
    bubbles = phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)")
    assert any("still fine" in b for b in bubbles), "the turn did not complete"
    assert phone.errors == []


def test_a_refused_wake_lock_does_not_break_the_turn(phone):
    """Low Power Mode rejects the request. That is not a reason to fail."""
    phone.setup()
    phone.page.evaluate("""() => {
      Object.defineProperty(navigator, 'wakeLock', {
        configurable: true,
        get: () => ({ request: async () => { throw new Error('denied'); } }),
      });
    }""")
    phone.reply({"role": "assistant", "content": "still fine"})
    phone.send("do something")
    phone.wait_idle()
    bubbles = phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)")
    assert any("still fine" in b for b in bubbles), "a refused wake lock killed the turn"
    assert phone.errors == []


def test_the_screen_lock_is_taken_again_on_the_way_back(phone):
    """The lock is dropped whenever the page is hidden and does not return by
    itself, so a turn still running would be unprotected from then on."""
    phone.setup()
    install_wake_lock_spy(phone)
    phone.page.evaluate("() => { window.__holdNext = true; }")
    phone.reply({"role": "assistant", "content": "done"})
    phone.send("something slow")
    phone.page.wait_for_function("() => window.__inFlight === true", timeout=15000)
    before = wake(phone)["requested"]
    hide(phone)
    unhide(phone)
    assert wake(phone)["requested"] > before, \
        "the screen was left free to lock for the rest of the turn"
    phone.page.evaluate("() => window.__release && window.__release()")
    phone.wait_idle()
    assert phone.errors == []


# ------------------------------------------- a suspension costs only the turn

def test_a_turn_killed_while_hidden_is_picked_up_on_the_way_back(phone):
    """The whole complaint: the agent is working, the app loses focus, and it
    stops and fails. It should carry on instead."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "the finished answer"})
    hide(phone)
    phone.page.evaluate("() => { window.__failNext = true; }")
    phone.send("do the work")
    phone.wait_idle()
    assert not resumed(phone), "resumed while still in the background"

    unhide(phone)
    phone.wait_idle()
    assert resumed(phone), "coming back to the app did not carry the turn on"
    bubbles = phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)")
    assert any("the finished answer" in b for b in bubbles), \
        "the resumed turn produced no answer"

    # And the flag is cleared, or every trip to the home screen from here on
    # would start the turn again.
    sent = requests_made(phone)
    hide(phone)
    unhide(phone)
    phone.page.wait_for_timeout(400)
    assert requests_made(phone) == sent, \
        "the interrupted flag was left set, so it resumes on every return"
    assert phone.errors == []


def test_leaving_the_app_mid_turn_is_the_case_that_matters(phone):
    """Not "background the app and then send" -- nobody does that. The real
    shape is sending something and then leaving, so whether the app was hidden
    has to be watched for the whole turn rather than sampled when it starts."""
    phone.setup()
    phone.page.evaluate("() => { window.__holdNext = true; }")
    phone.reply({"role": "assistant", "content": "the finished answer"})
    phone.send("do the work")
    phone.page.wait_for_function("() => window.__inFlight === true", timeout=15000)

    # ...and only now does the phone go away, with the request in flight.
    hide(phone)
    phone.page.evaluate("() => { window.__failNext = true; }")
    phone.page.evaluate("() => window.__release && window.__release()")
    phone.wait_idle()

    unhide(phone)
    phone.wait_idle()
    assert resumed(phone), "a turn abandoned part-way through was not picked up"
    bubbles = phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)")
    assert any("the finished answer" in b for b in bubbles)
    assert phone.errors == []


def test_a_real_error_in_the_foreground_is_not_treated_as_a_suspension(phone):
    """A rejected key or a bad request fails the same way a killed request does.
    Resuming that would call the same endpoint and fail again, forever."""
    phone.setup()
    phone.page.evaluate("() => { window.__failNext = true; }")
    phone.send("do the work")
    phone.wait_idle()
    errors = phone.page.eval_on_selector_all(
        ".bubble.error", "els => els.map(e => e.textContent)")
    assert errors, "a genuine error was swallowed instead of being shown"

    sent = requests_made(phone)
    hide(phone)
    unhide(phone)
    phone.page.wait_for_timeout(400)
    assert not resumed(phone), \
        "a foreground failure was marked resumable, which is a retry loop"
    assert requests_made(phone) == sent
    assert phone.errors == []


def test_the_killed_request_is_not_reported_as_an_error(phone):
    """It is not a fault worth showing; the app says it is carrying on instead."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "the finished answer"})
    hide(phone)
    phone.page.evaluate("() => { window.__failNext = true; }")
    phone.send("do the work")
    phone.wait_idle()
    errors = phone.page.eval_on_selector_all(
        ".bubble.error", "els => els.map(e => e.textContent)")
    assert errors == [], f"the suspension was reported as a failure: {errors}"
    assert phone.errors == []


def test_a_completed_turn_is_never_marked_interrupted(phone):
    """Backgrounding the app after an answer has landed must not cause the whole
    turn to be run a second time."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "all done"})
    hide(phone)
    phone.send("do the work")
    phone.wait_idle()
    sent = requests_made(phone)
    unhide(phone)
    phone.page.wait_for_timeout(400)
    assert not resumed(phone), \
        "a turn that reached its answer was queued to run again"
    assert requests_made(phone) == sent, \
        "returning to the app started an unwanted turn"
    assert phone.errors == []


def test_a_stopped_turn_is_not_resumed(phone):
    """Stop means stop, even if the app was in the background at the time."""
    phone.setup()
    phone.page.evaluate("() => { window.__holdNext = true; }")
    phone.reply({"role": "assistant", "content": "should never arrive"})
    phone.send("something slow")
    phone.page.wait_for_function("() => window.__inFlight === true", timeout=15000)
    phone.page.click("#btn-stop")
    hide(phone)
    phone.page.evaluate("() => window.__release && window.__release()")
    phone.wait_idle()
    sent = requests_made(phone)
    unhide(phone)
    phone.page.wait_for_timeout(400)
    assert not resumed(phone), "a deliberately stopped turn was queued to resume"
    assert requests_made(phone) == sent
    assert phone.errors == []
