"""Steering, retry and edit -- driven through the real app.

agent-core.test.js proves the steering hook injects between steps. It cannot
prove the composer stays usable mid-run, that the queued bubble appears and
clears, or that Edit/Retry are offered only on the tail, because none of that
exists outside the DOM. That was checked by hand once and then forgotten; this
is the same check, kept.
"""

import pytest


@pytest.fixture
def running(phone):
    """A phone with a turn stalled mid-flight, releasable on demand."""
    phone.setup()
    phone.page.evaluate("() => { window.__holdNext = true; }")
    phone.reply(
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function", "function": {
             "name": "read_file", "arguments": '{"path": "a.py"}'}}]},
        {"role": "assistant", "content": "done"},
    )
    phone.send("refactor the parser")
    phone.page.wait_for_function("() => window.__inFlight === true", timeout=15000)
    return phone


def click_tail_action(phone, label):
    """Click the Edit/Retry button by its label -- they share a class and are
    told apart only by text, the same way a person tells them apart."""
    phone.page.evaluate("""(want) => {
      const b = [...document.querySelectorAll('.tail-action')]
        .find((el) => el.textContent === want);
      if (!b) throw new Error('no tail action labelled ' + want);
      b.click();
    }""", label)


def release(phone):
    phone.page.evaluate("() => window.__release && window.__release()")
    phone.wait_idle()


def test_composer_stays_usable_while_a_turn_runs(running):
    p = running.page
    state = p.evaluate("""() => ({
      promptEnabled: !document.getElementById('in-prompt').disabled,
      placeholder: document.getElementById('in-prompt').placeholder,
      stopVisible: !document.getElementById('btn-stop').hidden,
    })""")
    assert state["promptEnabled"], "the composer locked during a run"
    assert state["stopVisible"]
    assert "Add something" in state["placeholder"]
    release(running)
    assert running.errors == []


def test_a_steer_is_queued_then_consumed_and_reaches_the_model(running):
    p = running.page
    p.fill("#in-prompt", "also rename it to Lexer")
    p.click("#btn-send")
    assert p.eval_on_selector_all(".bubble.user.queued", "e => e.length") == 1, \
        "a mid-run message should show as queued, not as a sent bubble"

    release(running)

    assert p.eval_on_selector_all(".bubble.user.queued", "e => e.length") == 0, \
        "the queued marker should clear once the steer goes in"
    sent = running.sent_messages()
    assert any(m.get("role") == "user" and "Lexer" in str(m.get("content", "")) for m in sent), \
        f"the steer never reached the model: {sent}"
    assert running.errors == []


def test_edit_and_retry_are_offered_only_on_the_tail(phone):
    """Deliberate: rewinding mid-conversation would strand file changes the
    agent already made, and the phone has no snapshot to revert to."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "first answer"})
    phone.send("first question")
    phone.wait_idle()
    phone.reply({"role": "assistant", "content": "second answer"})
    phone.send("second question")
    phone.wait_idle()

    counts = phone.page.evaluate("""() => {
      const users = [...document.querySelectorAll('.bubble.user')];
      const bots = [...document.querySelectorAll('.bubble.assistant')];
      const label = (el, want) => !!(el && [...el.querySelectorAll('.tail-action')]
        .some((b) => b.textContent === want));
      return {
        users: users.length, bots: bots.length,
        editOnLast: label(users[users.length - 1], 'Edit'),
        editOnFirst: label(users[0], 'Edit'),
        retryOnLast: label(bots[bots.length - 1], 'Retry'),
        retryOnFirst: label(bots[0], 'Retry'),
      };
    }""")
    assert counts["users"] == 2 and counts["bots"] == 2
    assert counts["editOnLast"] and not counts["editOnFirst"]
    assert counts["retryOnLast"] and not counts["retryOnFirst"]
    assert phone.errors == []


def test_retry_replaces_the_last_answer_instead_of_appending(phone):
    phone.setup()
    phone.reply({"role": "assistant", "content": "first answer"})
    phone.send("a question")
    phone.wait_idle()

    phone.reply({"role": "assistant", "content": "second attempt"})
    click_tail_action(phone, "Retry")
    phone.wait_idle()

    texts = phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)")
    assert len(texts) == 1, f"retry appended instead of replacing: {texts}"
    assert "second attempt" in texts[0]
    assert phone.errors == []


def test_edit_pulls_the_message_back_into_the_composer(phone):
    phone.setup()
    phone.reply({"role": "assistant", "content": "an answer"})
    phone.send("teh parser")
    phone.wait_idle()

    click_tail_action(phone, "Edit")
    assert phone.page.input_value("#in-prompt") == "teh parser"
    assert phone.page.eval_on_selector_all(".bubble.user", "e => e.length") == 0, \
        "the edited message should leave the transcript, not be duplicated"
    assert phone.errors == []
