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


def test_a_chat_without_a_repo_is_refused_not_adopted(phone):
    """The reported bug. The phone had you/app open; the desktop chat is about
    a local folder with no GitHub repo at all."""
    phone.setup()                       # leaves you/app connected
    seed_foreign_chat(phone, DESKTOP_CHAT)
    open_chat_named(phone, "Local notes chat")

    state = phone.page.evaluate("""() => ({
      onChatScreen: !document.getElementById('screen-chat').hidden,
      error: document.getElementById('chats-error').textContent,
    })""")
    assert not state["onChatScreen"], \
        "the chat opened against whichever repo the phone had open"
    assert "repository" in state["error"].lower(), \
        f"refused, but without saying why: {state['error']!r}"
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
