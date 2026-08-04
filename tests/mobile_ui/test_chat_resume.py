"""Resuming a synced chat on the phone.

The regression this file exists for: opening a synced chat loaded the queue of
work parked for the desktop and then immediately assigned an empty array over
it. The next save pushed that empty queue back to the store, so the parked work
was erased on every device -- silently, and permanently.

Everything here is asserted by reading the store back through an independent
reader, the way the other device would, rather than by peeking at the app's own
state. The in-memory copy being right is not the property that matters.
"""


def park_work(phone, task="run the integration tests", why="needs a real machine"):
    """Have the model park a task for the desktop, the way it does in practice."""
    phone.reply(
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "p1", "type": "function", "function": {
             "name": "needs_desktop",
             "arguments": '{"task": "%s", "why": "%s"}' % (task, why)}}]},
        {"role": "assistant", "content": "Parked that for your computer."},
    )
    phone.send(task)
    phone.wait_idle()


def reopen_through_the_hub(phone):
    p = phone.page
    p.click("#btn-back-repo")
    p.wait_for_selector("#screen-chats:not([hidden])", timeout=15000)
    p.click(".chat-row-main")
    p.wait_for_selector("#screen-chat:not([hidden])", timeout=15000)


def test_parked_work_survives_reopening_and_saving_again(phone):
    phone.setup()
    park_work(phone)

    chats = phone.stored_chats()
    assert len(chats) == 1
    assert [p["task"] for p in chats[0]["pending"]] == ["run the integration tests"]

    reopen_through_the_hub(phone)

    # Any further turn triggers a save -- and that save is what used to write
    # the emptied queue back over the good one.
    phone.reply({"role": "assistant", "content": "sure"})
    phone.send("thanks")
    phone.wait_idle()

    chats = phone.stored_chats()
    assert len(chats) == 1
    pending = chats[0]["pending"]
    assert [p["task"] for p in pending] == ["run the integration tests"], (
        f"reopening erased the work parked for the desktop: {pending}")
    assert phone.errors == []


def test_a_resumed_chat_keeps_its_history(phone):
    """The plain case, so a resume regression can't hide behind the pending one."""
    phone.setup()
    phone.reply({"role": "assistant", "content": "first answer"})
    phone.send("first question")
    phone.wait_idle()

    reopen_through_the_hub(phone)

    bubbles = phone.page.eval_on_selector_all(
        "#messages .bubble", "els => els.map(e => e.textContent)")
    assert any("first question" in b for b in bubbles)
    assert any("first answer" in b for b in bubbles)
    assert phone.errors == []
