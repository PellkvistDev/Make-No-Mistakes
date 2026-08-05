"""Edit & resend, and Regenerate, both address a past turn by counting bubbles.

This is the highest-stakes thing the desktop UI does: rewind_to reverts the
project files on disk to that turn's pre-turn snapshot. Naming the wrong turn
doesn't produce an error, it produces a plausible-looking rewind to somewhere
else and files reverted to the wrong point.

The UI sends `[...document.querySelectorAll(".msg-user")].indexOf(bubble)`.
The backend resolves that against `turn_ordinal` from to_display(), which
deliberately does NOT count steering, compaction summaries or internal nudges
-- they `continue` before the user append. So the two agree only as long as
none of those render as a .msg-user bubble. That is an invariant across two
files in two languages, held together by nothing but both sides being written
carefully, which is the kind that drifts.
"""

# The shapes to_display() actually emits, in an order that puts every
# non-counting kind between two real turns.
HISTORY = [
    {"kind": "user", "text": "first real turn"},
    {"kind": "assistant", "text": "ok"},
    {"kind": "steered", "text": "actually, use tabs"},
    {"kind": "compacted", "summary": "earlier turns summarised"},
    {"kind": "note", "text": "some note"},
    {"kind": "user", "text": "second real turn"},
    {"kind": "assistant", "text": "done"},
    {"kind": "user", "text": "third real turn"},
]


def render(desktop, items):
    desktop.boot()
    desktop.page.evaluate("(items) => { clearChatDom(); renderHistory(items, []); }", items)


def test_only_real_turns_render_as_user_bubbles(desktop):
    """The counting rule, stated where a future edit would break it."""
    render(desktop, HISTORY)
    texts = desktop.page.eval_on_selector_all(
        ".msg-user", "els => els.map(e => e.innerText)")
    assert len(texts) == 3, f"something that isn't a turn rendered as one: {texts}"
    assert "actually, use tabs" not in " ".join(texts), "a steered note counted as a turn"
    assert desktop.errors == []


def test_the_ordinal_sent_matches_the_turn_it_names(desktop):
    """What the UI computes for each bubble must equal that turn's ordinal.

    Off by one here reverts the project to the wrong snapshot, quietly.
    """
    render(desktop, HISTORY)
    ordinals = desktop.page.evaluate("""() => {
      const all = [...document.querySelectorAll('.msg-user')];
      return all.map((el) => ({
        text: el.innerText.trim().split('\\n')[0],
        ordinal: all.indexOf(el),
      }));
    }""")
    assert [o["ordinal"] for o in ordinals] == [0, 1, 2], ordinals
    assert ordinals[0]["text"].startswith("first")
    assert ordinals[1]["text"].startswith("second")
    assert ordinals[2]["text"].startswith("third")
    assert desktop.errors == []


def test_steering_between_turns_does_not_shift_later_ordinals(desktop):
    """The specific drift: a steered note before a turn must not push it up.

    Compare a history with the non-counting kinds against the same history
    without them -- every real turn must keep the same ordinal.
    """
    render(desktop, HISTORY)
    with_extras = desktop.page.eval_on_selector_all(
        ".msg-user", "els => els.map(e => e.innerText.trim().split('\\n')[0])")

    only_turns = [it for it in HISTORY if it["kind"] in ("user", "assistant")]
    render(desktop, only_turns)
    without = desktop.page.eval_on_selector_all(
        ".msg-user", "els => els.map(e => e.innerText.trim().split('\\n')[0])")

    assert with_extras == without, (
        "the non-counting kinds changed which turn each position names:\n"
        f"  with:    {with_extras}\n  without: {without}")
    assert desktop.errors == []


def test_regenerate_walks_back_to_the_right_prompt(desktop):
    """Regenerate finds its prompt by walking backwards to the nearest
    .msg-user. A steered note sitting between the reply and its prompt must not
    be mistaken for one."""
    render(desktop, HISTORY)
    found = desktop.page.evaluate("""() => {
      const bots = [...document.querySelectorAll('.msg-assistant, .assistant-block')];
      const last = bots[bots.length - 1];
      if (!last) return null;
      let el = last.previousElementSibling;
      while (el && !el.classList.contains('msg-user')) el = el.previousElementSibling;
      return el ? el.innerText.trim().split('\\n')[0] : null;
    }""")
    assert found is not None, "no prompt found walking back from the last reply"
    assert found.startswith("second"), f"walked back to the wrong prompt: {found!r}"
    assert desktop.errors == []
