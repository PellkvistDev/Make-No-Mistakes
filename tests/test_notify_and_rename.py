"""The ~/.makenomistakes rename (with one-time migration of the old
~/.glmcode dir) and OS-level attention notifications."""

import sys
import types

import pytest

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object()))

from glmcode import config, notify  # noqa: E402
from glmcode.gui import app as gui_app  # noqa: E402


# -- data-dir rename ------------------------------------------------------ #

def test_config_dir_is_makenomistakes():
    assert config.CONFIG_DIR.name == ".makenomistakes"


def test_migration_moves_legacy_dir(tmp_path):
    old, new = tmp_path / ".glmcode", tmp_path / ".makenomistakes"
    (old / "sessions").mkdir(parents=True)
    (old / "memory.md").write_text("remember me", encoding="utf-8")
    (old / "sessions" / "a.json").write_text("{}", encoding="utf-8")

    assert config.migrate_legacy_dir(old, new) is True
    assert not old.exists()
    assert (new / "memory.md").read_text(encoding="utf-8") == "remember me"
    assert (new / "sessions" / "a.json").exists()


def test_migration_never_clobbers_existing_new_dir(tmp_path):
    old, new = tmp_path / ".glmcode", tmp_path / ".makenomistakes"
    old.mkdir()
    (old / "memory.md").write_text("old", encoding="utf-8")
    new.mkdir()
    (new / "memory.md").write_text("new", encoding="utf-8")

    assert config.migrate_legacy_dir(old, new) is False
    # both untouched
    assert (old / "memory.md").read_text(encoding="utf-8") == "old"
    assert (new / "memory.md").read_text(encoding="utf-8") == "new"


def test_migration_noop_without_legacy_dir(tmp_path):
    assert config.migrate_legacy_dir(
        tmp_path / ".glmcode", tmp_path / ".makenomistakes") is False
    assert not (tmp_path / ".makenomistakes").exists()


# -- notification commands ------------------------------------------------ #

def test_linux_uses_notify_send():
    cmd = notify._command("Chat", "Done -- waiting for you.", platform="linux")
    assert cmd == ["notify-send", "--app-name", "Make No Mistakes",
                   "Chat", "Done -- waiting for you."]


def test_windows_toast_escapes_xml_and_quotes():
    cmd = notify._command("It's <b>ad", 'say "hi" & bye', platform="win32")
    assert cmd[0] == "powershell"
    script = cmd[-1]
    # XML-escaped inside the toast payload...
    assert "&lt;b&gt;ad" in script and "&quot;hi&quot; &amp; bye" in script
    # ...and the ' from the title doubled for the PS single-quoted literal
    # (it lands as &apos; first, whose own quotes need no doubling).
    assert "&apos;" in script
    assert "<b>" not in script


def test_mac_osascript_escapes_double_quotes():
    cmd = notify._command('say "hi"', "b", platform="darwin")
    assert cmd[0] == "osascript"
    assert '\\"hi\\"' in cmd[2]


def test_run_swallows_missing_binary():
    notify._run(["definitely-not-a-real-command-xyz"])  # must not raise


# -- wiring --------------------------------------------------------------- #

def test_os_attention_only_fires_when_unfocused(monkeypatch):
    calls = []
    monkeypatch.setattr(gui_app, "notify", lambda t, b: calls.append((t, b)))
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = types.SimpleNamespace(notifications=True)
    api._chats = {"s1": types.SimpleNamespace(title="Fix the bug")}

    api._window_focused = True
    api._os_attention("s1", "Needs permission: run command")
    assert calls == []

    api._window_focused = False
    api._os_attention("s1", "Needs permission: run command")
    api._os_attention("unknown-sid", "Done -- waiting for you.")
    assert calls == [("Fix the bug", "Needs permission: run command"),
                     ("Make No Mistakes", "Done -- waiting for you.")]


def test_os_attention_respects_settings_toggle(monkeypatch):
    calls = []
    monkeypatch.setattr(gui_app, "notify", lambda t, b: calls.append((t, b)))
    api = gui_app.Api.__new__(gui_app.Api)
    api._cfg = types.SimpleNamespace(notifications=False)
    api._chats = {}
    api._window_focused = False
    api._os_attention("s1", "Needs permission: run command")
    assert calls == []


def test_notifications_default_on():
    assert config.Config().notifications is True
