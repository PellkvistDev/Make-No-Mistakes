"""Edit & resend: rewinding the chat to a past user turn -- truncating the
conversation there and reverting the project files to that turn's shadow-git
snapshot -- plus the turn-ordinal <-> snapshot mapping that drives it."""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402
from glmcode.sessions import to_display  # noqa: E402


# -- to_display tags every user turn with its ordinal + position ---------- #

def test_to_display_tags_user_turns():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "reply two"},
    ]
    items = to_display(msgs)
    users = [it for it in items if it["kind"] == "user"]
    assert [u["text"] for u in users] == ["first", "second"]
    assert [u["turn_ordinal"] for u in users] == [0, 1]
    # absolute positions point back into the ORIGINAL list (system included)
    assert users[0]["msg_index"] == 1 and users[1]["msg_index"] == 3
    assert msgs[users[1]["msg_index"]]["content"] == "second"


# -- rewind_to ------------------------------------------------------------ #

class FakeAgent:
    def __init__(self, messages):
        self.messages = messages
        self.busy = False
        self.todos = [{"content": "stale", "status": "pending"}]
        from glmcode.config import Config
        self.session_usage = types.SimpleNamespace(prompt_tokens=5, completion_tokens=9)
        self.workdir = __import__("pathlib").Path(".")
        self.cfg = Config()

    def context_estimate(self):
        return 0


class FakeBackup:
    def __init__(self):
        self.reverted_to = None

    def revert_to(self, commit):
        self.reverted_to = commit


def make_api_with_chat(messages, turn_snapshots, backup=None, auto_backup=True):
    api = gui_app.Api.__new__(gui_app.Api)
    api._chats = {}
    api.session_id = "s1"
    agent = FakeAgent(messages)
    cs = gui_app.ChatState.__new__(gui_app.ChatState)
    cs.sid = "s1"
    cs.agent = agent
    cs.backup_repo = backup
    cs.auto_backup = auto_backup
    cs.title = "t"
    cs.provider = ""
    cs.model = ""
    cs.turn_snapshots = list(turn_snapshots)
    api._chats["s1"] = cs
    # stub out persistence + payload data-uri work
    api._store = types.SimpleNamespace(save=lambda *a, **k: None)
    return api, cs, agent


def three_turn_convo():
    # system + 3 user turns each followed by an assistant reply
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "turn 2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_rewind_truncates_and_reverts_files():
    backup = FakeBackup()
    api, cs, agent = make_api_with_chat(
        three_turn_convo(),
        [{"commit": "c0"}, {"commit": "c1"}, {"commit": "c2"}],
        backup=backup)
    res = api.rewind_to(1)  # edit "turn 1"
    assert "error" not in res
    # conversation truncated to before turn 1 (system + turn0 + a0)
    assert [m.get("content") for m in agent.messages] == ["sys", "turn 0", "a0"]
    # files reverted to turn 1's pre-turn snapshot
    assert backup.reverted_to == "c1"
    assert res["reverted"] is True
    # snapshot map truncated to the surviving turns
    assert cs.turn_snapshots == [{"commit": "c0"}]
    # stale todos cleared
    assert agent.todos == []


def test_rewind_to_first_turn_reverts_to_original_state():
    backup = FakeBackup()
    api, cs, agent = make_api_with_chat(
        three_turn_convo(),
        [{"commit": "c0"}, {"commit": "c1"}, {"commit": "c2"}], backup=backup)
    res = api.rewind_to(0)
    assert backup.reverted_to == "c0"
    assert [m.get("content") for m in agent.messages] == ["sys"]
    assert cs.turn_snapshots == []


def test_rewind_without_snapshot_still_truncates_but_flags_no_revert():
    backup = FakeBackup()
    # backups were off for these turns -> commit None
    api, cs, agent = make_api_with_chat(
        three_turn_convo(),
        [{"commit": None}, {"commit": None}, {"commit": None}], backup=backup)
    res = api.rewind_to(1)
    assert backup.reverted_to is None       # nothing to revert to
    assert res["reverted"] is False
    assert res["had_snapshot"] is False
    assert [m.get("content") for m in agent.messages] == ["sys", "turn 0", "a0"]


def test_rewind_refuses_while_busy():
    api, cs, agent = make_api_with_chat(three_turn_convo(), [{"commit": "c0"}])
    agent.busy = True
    assert "working" in api.rewind_to(0)["error"]
    assert len(agent.messages) == 7  # untouched


def test_rewind_bad_ordinal_errors():
    api, cs, agent = make_api_with_chat(three_turn_convo(),
                                        [{"commit": "c0"}, {"commit": "c1"}, {"commit": "c2"}])
    assert "no longer available" in api.rewind_to(9)["error"]
    assert len(agent.messages) == 7


def test_rewind_ignores_steering_messages_in_ordinal():
    from glmcode.prompts import STEER_NUDGE_TEMPLATE
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn 0"},
        {"role": "assistant", "content": "a0"},
        # a steering message is user-role but NOT a real turn
        {"role": "user", "content": STEER_NUDGE_TEMPLATE.format(text="hint")},
        {"role": "assistant", "content": "a0b"},
        {"role": "user", "content": "turn 1"},
        {"role": "assistant", "content": "a1"},
    ]
    backup = FakeBackup()
    api, cs, agent = make_api_with_chat(msgs, [{"commit": "c0"}, {"commit": "c1"}],
                                        backup=backup)
    # ordinal 1 must map to the REAL second turn ("turn 1"), skipping the
    # steering message -- and revert to that turn's snapshot c1.
    res = api.rewind_to(1)
    assert backup.reverted_to == "c1"
    assert [m.get("content") for m in agent.messages][-1] == "a0b"
    assert not any(m.get("content") == "turn 1" for m in agent.messages)
