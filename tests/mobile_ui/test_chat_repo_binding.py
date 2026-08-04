"""A chat belongs to one repository, and the phone must not decide which.

Reported: a chat started on the desktop showed up on the phone, answered a
message, ran tools, and got relabelled to an unrelated GitHub repo.

The chain was: desktop chats carried no `repo` at all; the phone's guard only
caught a phone with NO repo open, so a leftover one from an earlier
conversation was silently adopted; the agent then read and committed into a
codebase the conversation was never about; and the next save wrote that repo
back to the shared store, relabelling the chat on every device.
"""

import json


def seed_foreign_chat(phone, chat, passphrase="sync passphrase"):
    """Put a chat into the store as if another device had written it."""
    phone.page.evaluate("""async ([pass, chat]) => {
      const AC = window.AgentCore;
      const probe = AC.makeGitHub({ token: "ghtoken", owner: "", repo: "" });
      const { owner, repo } = await AC.ensureSyncRepo(probe);
      const gh = AC.makeGitHub({ token: "ghtoken", owner, repo,
                                 branch: AC.SYNC_REPO_BRANCH });
      const { store } = await AC.openSync(gh, pass);
      await store.save(chat);
    }""", [passphrase, chat])


DESKTOP_CHAT = {
    "id": "from-desktop",
    "title": "Local notes chat",
    "project": "notes",
    "device": "desktop",
    # No `repo` -- exactly what every desktop chat used to look like.
    "messages": [{"role": "system", "content": ""},
                 {"role": "user", "content": "what did we decide?"}],
    "transcript": [{"role": "user", "text": "what did we decide?"}],
    "desktop": {"cwd": "/home/me/notes", "todos": ["ship it"],
                "model": "glm-4.7", "model_provider": "zai"},
}


def open_chat_named(phone, title):
    phone.page.click("#btn-back-repo")
    phone.page.wait_for_selector("#screen-chats:not([hidden])", timeout=15000)
    phone.page.wait_for_function(
        """(t) => [...document.querySelectorAll('.chat-row-title')]
             .some((el) => el.textContent === t)""",
        arg=title, timeout=15000)
    phone.page.evaluate("""(t) => {
      const row = [...document.querySelectorAll('.chat-row-title')]
        .find((el) => el.textContent === t);
      row.closest('.chat-row').querySelector('.chat-row-main').click();
    }""", title)
    phone.page.wait_for_timeout(600)


def test_a_chat_without_a_repo_is_never_bound_to_the_phones_own(phone):
    """The reported bug. The phone had you/app open; the desktop chat is about a
    local folder with no GitHub repo at all.

    It may open -- reading it is the point of syncing it -- but it must not
    become a working chat against you/app. The header naming the project rather
    than a repo, and the composer being a note box rather than a prompt, are how
    that is visible: there is no route from here to the agent.
    """
    phone.setup()                       # leaves you/app connected
    seed_foreign_chat(phone, DESKTOP_CHAT)
    open_chat_named(phone, "Local notes chat")

    state = phone.page.evaluate("""() => ({
      readOnly: document.getElementById('screen-chat').classList.contains('read-only'),
      header: document.getElementById('chat-repo-name').textContent,
    })""")
    assert state["readOnly"], "the chat opened as a working chat"
    assert state["header"] != "you/app", \
        "the chat was bound to whichever repo the phone had open"
    assert phone.errors == []


def test_refusing_leaves_the_stored_chat_untouched(phone):
    """Refusing has to be inert. The damage was never the opening -- it was the
    save afterwards writing the phone's repo, and the phone's shorter field set,
    over a chat it did not own."""
    phone.setup()
    seed_foreign_chat(phone, DESKTOP_CHAT)
    open_chat_named(phone, "Local notes chat")

    stored = next(c for c in phone.stored_chats() if c["id"] == "from-desktop")
    assert stored.get("repo") in (None, {}), f"a repo was written in: {stored.get('repo')}"
    assert stored["project"] == "notes", f"the chat was relabelled: {stored['project']}"
    assert stored["device"] == "desktop", "the chat was claimed by the phone"
    assert stored["desktop"]["cwd"] == "/home/me/notes", \
        "the desktop's working directory was destroyed"
    assert phone.errors == []


def test_a_chat_that_names_its_repo_is_followed_there(phone):
    """The other half: binding is right when the chat says where it belongs."""
    phone.setup()
    chat = dict(DESKTOP_CHAT, id="with-repo", title="Repo chat",
                repo={"owner": "you", "repo": "app", "full_name": "you/app",
                      "branch": "main"})
    seed_foreign_chat(phone, chat)
    open_chat_named(phone, "Repo chat")

    state = phone.page.evaluate("""() => ({
      onChatScreen: !document.getElementById('screen-chat').hidden,
      repoName: document.getElementById('chat-repo-name').textContent,
    })""")
    assert state["onChatScreen"], "a chat naming its own repo should open"
    assert state["repoName"] == "you/app"
    assert phone.errors == []


def test_answering_from_the_phone_keeps_the_desktop_fields(phone):
    """save() replaces the whole object, so anything the phone doesn't send is
    destroyed -- the desktop's cwd, todos and model choice among them."""
    phone.setup()
    chat = dict(DESKTOP_CHAT, id="with-repo", title="Repo chat",
                repo={"owner": "you", "repo": "app", "full_name": "you/app",
                      "branch": "main"})
    seed_foreign_chat(phone, chat)
    open_chat_named(phone, "Repo chat")

    phone.reply({"role": "assistant", "content": "we decided to ship"})
    phone.send("remind me")
    phone.wait_idle()

    stored = next(c for c in phone.stored_chats() if c["id"] == "with-repo")
    assert stored["desktop"]["cwd"] == "/home/me/notes", \
        f"the phone wiped the desktop's working directory: {stored.get('desktop')}"
    assert stored["desktop"]["todos"] == ["ship it"]
    assert stored["project"] == "notes", \
        f"answering from the phone relabelled the chat: {stored['project']}"
    assert phone.errors == []


def test_a_local_only_chat_is_marked_in_the_list_before_you_tap_it(phone):
    """Refusing on tap is correct but late. A chat that can't be continued here
    should not look identical to one that can."""
    phone.setup()
    seed_foreign_chat(phone, DESKTOP_CHAT)
    seed_foreign_chat(phone, dict(
        DESKTOP_CHAT, id="with-repo", title="Repo chat",
        repo={"owner": "you", "repo": "app", "full_name": "you/app", "branch": "main"}))

    phone.page.click("#btn-back-repo")
    phone.page.wait_for_selector("#screen-chats:not([hidden])", timeout=15000)
    phone.page.wait_for_function(
        "() => document.querySelectorAll('.chat-row').length === 2", timeout=15000)

    rows = phone.page.evaluate("""() => {
      const out = {};
      for (const li of document.querySelectorAll('.chat-row')) {
        const title = li.querySelector('.chat-row-title').textContent;
        const tag = li.querySelector('.chat-row-tag');
        out[title] = { local: li.classList.contains('chat-row-local'),
                       tag: tag ? tag.textContent : null };
      }
      return out;
    }""")
    assert rows["Local notes chat"]["local"], "the local-only chat is not marked"
    assert rows["Local notes chat"]["tag"] == "on your computer"
    assert not rows["Repo chat"]["local"], "a chat that works here was marked as local"
    assert rows["Repo chat"]["tag"] is None
    assert phone.errors == []


def test_the_marker_survives_a_title_too_long_to_fit(phone):
    """The title is a single ellipsised line, so the label cannot live inside
    it -- it would be truncated away exactly where knowing matters most."""
    phone.setup()
    seed_foreign_chat(phone, dict(
        DESKTOP_CHAT, id="longy",
        title="A conversation with a really quite extraordinarily long title "
              "that will not fit on a phone screen at all"))
    phone.page.click("#btn-back-repo")
    phone.page.wait_for_selector(".chat-row-tag", timeout=15000)
    visible = phone.page.evaluate("""() => {
      const tag = document.querySelector('.chat-row-tag');
      const row = tag.closest('.chat-row');
      const t = tag.getBoundingClientRect(), r = row.getBoundingClientRect();
      return t.width > 0 && t.right <= r.right + 1 && t.left >= r.left - 1;
    }""")
    assert visible, "the marker was clipped off the row"
    assert phone.errors == []


# ------------------------------------------------------------- read-only --
# Marking a chat you can't use is only half an answer: a labelled dead end is
# still a dead end. These chats sync, so they should at least be readable, and
# the one useful thing you can do without a repo is leave work for the machine
# that has one.

def test_a_local_only_chat_opens_for_reading(phone):
    phone.setup()
    seed_foreign_chat(phone, dict(DESKTOP_CHAT, transcript=[
        {"role": "user", "text": "what did we settle on for the schema?"},
        {"role": "assistant", "text": "One table, keyed by run id."}]))
    open_chat_named(phone, "Local notes chat")

    state = phone.page.evaluate("""() => ({
      onChatScreen: !document.getElementById('screen-chat').hidden,
      readOnly: document.getElementById('screen-chat').classList.contains('read-only'),
      bubbles: [...document.querySelectorAll('.bubble')].map((b) => b.textContent),
      placeholder: document.getElementById('in-prompt').placeholder,
      attachHidden: document.getElementById('btn-attach').hidden,
    })""")
    assert state["onChatScreen"], "a chat that syncs should at least be readable"
    assert state["readOnly"]
    assert any("keyed by run id" in b for b in state["bubbles"]), "the transcript isn't shown"
    assert "note for your computer" in state["placeholder"]
    assert state["attachHidden"], "nothing can be attached without a repo"
    assert phone.errors == []


def test_a_note_left_here_waits_on_the_computer(phone):
    """The composer is repurposed, not disabled: what you send becomes a task in
    the same queue needs_desktop writes, so the desktop surfaces it on open."""
    phone.setup()
    seed_foreign_chat(phone, DESKTOP_CHAT)
    open_chat_named(phone, "Local notes chat")

    phone.page.fill("#in-prompt", "run the migration script")
    phone.page.click("#btn-send")
    phone.page.wait_for_function(
        """() => [...document.querySelectorAll('.bubble')]
             .some((b) => b.textContent.includes('run the migration script'))""",
        timeout=15000)
    phone.page.wait_for_timeout(400)

    stored = next(c for c in phone.stored_chats() if c["id"] == "from-desktop")
    assert [p["task"] for p in stored["pending"]] == ["run the migration script"]
    # Only `pending` was ours to touch.
    assert stored["device"] == "desktop", "leaving a note claimed the chat for the phone"
    assert stored["project"] == "notes", "leaving a note relabelled the chat"
    assert stored["desktop"]["cwd"] == "/home/me/notes"
    assert phone.errors == []


def test_reading_a_local_chat_does_not_disturb_the_working_one(phone):
    """Read-only leaves session.repo alone, so the guards that key off it don't
    fire. Going back to a real chat has to still work."""
    phone.setup()
    seed_foreign_chat(phone, DESKTOP_CHAT)
    seed_foreign_chat(phone, dict(
        DESKTOP_CHAT, id="with-repo", title="Repo chat",
        repo={"owner": "you", "repo": "app", "full_name": "you/app", "branch": "main"}))
    open_chat_named(phone, "Local notes chat")
    open_chat_named(phone, "Repo chat")

    state = phone.page.evaluate("""() => ({
      readOnly: document.getElementById('screen-chat').classList.contains('read-only'),
      placeholder: document.getElementById('in-prompt').placeholder,
      attachHidden: document.getElementById('btn-attach').hidden,
      repoName: document.getElementById('chat-repo-name').textContent,
    })""")
    assert not state["readOnly"], "read-only chrome stuck on a working chat"
    assert state["placeholder"] == "Message the agent…"
    assert not state["attachHidden"]
    assert state["repoName"] == "you/app"

    phone.reply({"role": "assistant", "content": "still working"})
    phone.send("are you there")
    phone.wait_idle()
    assert any("still working" in b for b in phone.page.eval_on_selector_all(
        ".bubble.assistant", "els => els.map(e => e.textContent)"))
    assert phone.errors == []


def test_a_read_only_chat_offers_no_edit_or_retry(phone):
    """Caught by rendering it, not by an assertion: the tail actions were on
    the bubbles. There is no agent behind a read-only chat and its message list
    is empty, so both buttons lead nowhere."""
    phone.setup()
    seed_foreign_chat(phone, dict(DESKTOP_CHAT, transcript=[
        {"role": "user", "text": "where did we put it?"},
        {"role": "assistant", "text": "under receipts/2026"}]))
    open_chat_named(phone, "Local notes chat")

    actions = phone.page.eval_on_selector_all(
        ".tail-action", "els => els.map(e => e.textContent)")
    assert actions == [], f"read-only chat offered dead actions: {actions}"
    assert phone.errors == []
