"""Forking from a past message, driven through the real UI.

Edit-and-resend rewinds and DISCARDS everything after the message. Often what
you want is the other branch kept, so the two can be compared -- and the
pre-turn snapshot that makes that possible is already on disk.

The button sits beside the pencil, uses the same turn ordinal, and lands you in
the new chat.
"""

BASE = {"mode": "ask", "thinking_mode": "medium", "reduce_effects": True}


def _with_history(desktop):
    """A chat with three of the user's messages in it."""
    items = []
    for i, text in enumerate(("first question", "second question", "third question")):
        items.append({"kind": "user", "text": text, "turn_ordinal": i})
        items.append({"kind": "assistant", "text": f"answer {i}"})
    desktop.boot(boot={"settings": BASE, "sessions": [],
                       "session": {"id": "s1", "cwd": "/tmp/p", "items": items,
                                   "todos": [], "prompt_tokens": 0,
                                   "completion_tokens": 0}})
    p = desktop.page
    p.evaluate("""(items) => { clearChatDom(); renderHistory(items, []); }""", items)
    p.wait_for_timeout(150)
    return p


def _forks(p):
    return p.evaluate("() => document.querySelectorAll('.user-fork').length")


def test_every_message_you_sent_offers_a_fork(desktop):
    p = _with_history(desktop)
    assert _forks(p) == 3
    assert desktop.errors == []


def test_it_sits_beside_the_edit_button(desktop):
    """Same place, same discoverability -- they are two answers to the same
    impulse ("go back to here"), and separating them would hide one."""
    p = _with_history(desktop)
    rows = p.evaluate(
        """() => [...document.querySelectorAll('.msg-user')].map(m => ({
             edit: !!m.querySelector('.user-edit:not(.user-copy):not(.user-fork)'),
             fork: !!m.querySelector('.user-fork') }))""")
    assert all(r["edit"] and r["fork"] for r in rows)


def test_clicking_it_asks_the_backend_for_that_turn(desktop):
    """The ordinal is the bubble's position among user bubbles -- the same
    number the edit path sends, so the two cannot disagree about which
    message was meant."""
    p = _with_history(desktop)
    p.evaluate("""() => window.__reply('fork_at',
                    { id: 'fork1', cwd: '/tmp/p', items: [], todos: [],
                      prompt_tokens: 0, completion_tokens: 0,
                      forked_from: 's1', reverted: true, sessions: [] })""")

    p.evaluate("""() => document.querySelectorAll('.user-fork')[1].click()""")
    p.wait_for_timeout(300)

    calls = [c for c in p.evaluate("() => window.__calls") if c["name"] == "fork_at"]
    assert calls, "the backend was never asked to fork"
    assert calls[0]["args"][0] == 1, "the wrong message was forked"


def test_it_opens_the_new_chat(desktop):
    p = _with_history(desktop)
    p.evaluate("""() => window.__reply('fork_at',
                    { id: 'fork1', cwd: '/tmp/p', items: [], todos: [],
                      prompt_tokens: 0, completion_tokens: 0,
                      forked_from: 's1', reverted: true, sessions: [] })""")

    p.evaluate("""() => document.querySelectorAll('.user-fork')[0].click()""")
    p.wait_for_timeout(400)

    assert p.evaluate("() => activeSessionId") == "fork1"
    assert desktop.errors == []


def test_a_refusal_is_shown_and_nothing_moves(desktop):
    """The backend refuses while the agent is working. The UI must not pretend
    it forked."""
    p = _with_history(desktop)
    p.evaluate("""() => window.__reply('fork_at',
                    { error: "can't fork a chat while the agent is working" })""")

    p.evaluate("""() => document.querySelectorAll('.user-fork')[0].click()""")
    p.wait_for_timeout(300)

    assert p.evaluate("() => activeSessionId") != "fork1"
    body = p.evaluate("() => document.body.textContent")
    assert "while the agent is working" in body
