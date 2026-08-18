"""Branching a conversation at a past message, instead of only undoing to it.

backup.py commits a snapshot of the work tree before every single user turn,
and edit-and-resend already rewinds both the conversation and the files to one.
So the history underneath has always been a TREE; it was only ever offered as a
line, and rewinding DISCARDS the branch you were on.

For an agent that is wrong a fair share of the time, "try it both ways and
compare" is a better primitive than undo -- and the storage cost is zero,
because those commits are already being written.

Forking keeps the original chat exactly as it is and opens a new one holding
the conversation up to that point, with the files reverted to the same
snapshot. The two then diverge.
"""

import sys
import threading
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402


class _Backup:
    def __init__(self):
        self.reverted_to = []

    def revert_to(self, commit):
        self.reverted_to.append(commit)


class _Agent:
    busy = False

    def __init__(self, messages):
        self.messages = list(messages)
        self.workdir = "/tmp/project"
        self.todos = []


def _messages():
    """Three user turns with replies, the shape to_display maps."""
    out = []
    for i, text in enumerate(("first question", "second question", "third question")):
        out.append({"role": "user", "content": text})
        out.append({"role": "assistant", "content": f"answer {i}"})
    return out


def _api_with_chat(monkeypatch):
    api = gui_app.Api.__new__(gui_app.Api)
    cs = gui_app.ChatState.__new__(gui_app.ChatState)
    cs.sid = "parent"
    cs.agent = _Agent(_messages())
    cs.title = "Original"
    cs.provider, cs.model = "zai", "glm-4.7-flash"
    cs.auto_backup = True
    cs.turn_snapshots = [{"commit": "c0"}, {"commit": "c1"}, {"commit": "c2"}]
    cs.backup_repo = _Backup()
    cs.turn_lock = threading.Lock()
    api._chats = {"parent": cs}
    api.session_id = "parent"

    created = {}

    def fake_activate(sid, messages, cwd, pt, ct, todos, title="",
                      auto_backup=True, model_provider="", model="",
                      turn_snapshots=None):
        created.update({"sid": sid, "messages": messages, "cwd": cwd,
                        "title": title, "snapshots": turn_snapshots,
                        "model": model, "provider": model_provider})
        return {"id": sid, "items": [], "todos": []}

    monkeypatch.setattr(api, "_activate_session", fake_activate)
    monkeypatch.setattr(api, "list_sessions", lambda: [{"id": "parent"}, {"id": "new"}])
    return api, cs, created


# ----------------------------------------------- the original survives ----

def test_the_original_chat_is_left_exactly_as_it_was(monkeypatch):
    """The whole difference from rewind_to, which truncates in place."""
    api, cs, _ = _api_with_chat(monkeypatch)
    before = [dict(m) for m in cs.agent.messages]

    api.fork_at(1)

    assert cs.agent.messages == before
    assert len(cs.turn_snapshots) == 3


def test_the_fork_holds_the_conversation_up_to_that_message(monkeypatch):
    """Up to, not including: the fork starts where that turn was about to be
    sent, so its text is still yours to retype or change."""
    api, _cs, created = _api_with_chat(monkeypatch)

    api.fork_at(1)

    texts = [m["content"] for m in created["messages"]]
    assert texts == ["first question", "answer 0"]
    assert "second question" not in texts


def test_forking_at_the_first_message_gives_an_empty_chat(monkeypatch):
    api, _cs, created = _api_with_chat(monkeypatch)
    api.fork_at(0)
    assert created["messages"] == []


def test_the_fork_gets_its_own_id_and_a_name_that_says_what_it_is(monkeypatch):
    api, cs, created = _api_with_chat(monkeypatch)
    api.fork_at(1)
    assert created["sid"] != cs.sid
    assert "fork" in created["title"].lower()
    assert "Original" in created["title"]


def test_the_fork_inherits_the_folder_and_the_model(monkeypatch):
    """A branch of a conversation is about the same project, and switching
    model underneath it would change what is being compared."""
    api, cs, created = _api_with_chat(monkeypatch)
    api.fork_at(1)
    assert created["cwd"] == str(cs.agent.workdir)
    assert created["model"] == "glm-4.7-flash"
    assert created["provider"] == "zai"


# --------------------------------------------------------- the files -----

def test_the_project_is_reverted_to_that_turn_s_snapshot(monkeypatch):
    """Forking a conversation without the files is a branch that cannot be
    compared: the code on disk would still be the other branch's."""
    api, cs, _ = _api_with_chat(monkeypatch)
    res = api.fork_at(1)
    assert cs.backup_repo.reverted_to == ["c1"]
    assert res["reverted"] is True


def test_a_turn_with_no_snapshot_still_forks_and_says_so(monkeypatch):
    """Backups may have been off, or a compaction cleared the map. The
    conversation can still branch; the files just are not moved, and that is
    reported rather than implied."""
    api, cs, _ = _api_with_chat(monkeypatch)
    cs.turn_snapshots = []
    res = api.fork_at(1)
    assert res["reverted"] is False
    assert res["had_snapshot"] is False
    assert cs.backup_repo.reverted_to == []


def test_a_failed_revert_stops_before_a_chat_is_created(monkeypatch):
    """Half a fork -- a new chat whose files belong to the other branch -- is
    worse than none."""
    api, cs, created = _api_with_chat(monkeypatch)

    def boom(_commit):
        raise OSError("shadow repo is gone")

    cs.backup_repo.revert_to = boom
    res = api.fork_at(1)

    assert "error" in res
    assert created == {}, "a chat was created after the revert failed"


def test_the_fork_carries_the_snapshots_it_inherits(monkeypatch):
    """So the new chat can itself be rewound or forked again -- the tree keeps
    branching rather than flattening at the first fork."""
    api, _cs, created = _api_with_chat(monkeypatch)
    api.fork_at(2)
    assert [t["commit"] for t in created["snapshots"]] == ["c0", "c1"]


# ------------------------------------------------------- refusals -------

def test_it_will_not_fork_a_chat_that_is_working(monkeypatch):
    api, cs, created = _api_with_chat(monkeypatch)
    cs.agent.busy = True
    res = api.fork_at(1)
    assert "error" in res and "working" in res["error"]
    assert created == {}


def test_an_unknown_message_is_refused(monkeypatch):
    api, _cs, created = _api_with_chat(monkeypatch)
    assert "error" in api.fork_at(99)
    assert created == {}


def test_a_nonsense_reference_is_refused(monkeypatch):
    api, _cs, _ = _api_with_chat(monkeypatch)
    assert "error" in api.fork_at("banana")


def test_no_active_chat_is_an_error_not_a_crash():
    api = gui_app.Api.__new__(gui_app.Api)
    api._chats = {}
    api.session_id = ""
    assert "error" in api.fork_at(0)


def test_the_caller_is_told_which_chat_it_came_from(monkeypatch):
    """So the UI can say so, and so the two are relatable afterwards."""
    api, cs, _ = _api_with_chat(monkeypatch)
    res = api.fork_at(1)
    assert res["forked_from"] == cs.sid
