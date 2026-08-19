"""The Api class is being taken apart along real seams, not at a line count.

gui/app.py had grown to a single Api class of 190 methods across ~3,600 lines,
and every new feature landed in it. Two seams have been cut so far, each a
subject with its own vocabulary rather than a convenient length:

  - DeviceApi  -- sync, pairing, Web Push, the CI runner: talking to a machine
                  that is not this one.
  - VoiceApi   -- speech-to-text, text-to-speech and the spoken conversation:
                  the delegator agent, its own event sink, and the queues that
                  carry work done by voice back to the coding agent.
  - GitHubApi  -- cloning, connecting, the token, push and pull, and reviewing
                  a pull request: everything that speaks `githubsync` about the
                  chat's own repository.

What these tests protect is the property that makes the split safe to repeat:
the bridge JavaScript calls must be unchanged. pywebview exposes the Api
instance's public methods by inspection, so a method that moved to a mixin is
still found -- but a method that moved to a *collaborator object* would not be,
and would fail only at runtime, in the app, on the one path nobody re-tests.
"""

import ast
import pathlib
import sys
import types

sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode.gui import app as gui_app  # noqa: E402
from glmcode.gui import devices_api  # noqa: E402
from glmcode.gui import github_api  # noqa: E402
from glmcode.gui import voice_api  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUI = ROOT / "glmcode" / "gui"
APP = GUI / "app.py"

# Every method the frontend reaches on each subject. Named rather than derived,
# because the point is that this exact list keeps working -- a rule computed
# from the code would move when the code did.
DEVICE_BRIDGE = [
    "sync_env", "sync_enable", "sync_recovery_code", "sync_set_passphrase",
    "sync_forget_passphrase", "sync_list_chats", "sync_finish_interrupted",
    "sync_pull_chat", "sync_push_chat", "sync_catch_up", "sync_delete_chat",
    "webpush_public_key", "ci_status", "ci_install", "ci_dispatch",
    # Setting the phone up IS reaching the other machine. These two were left
    # in app.py by the first split and belong here.
    "get_phone_app", "get_pair_phone",
]
VOICE_BRIDGE = [
    "voice_mode", "voice_ack", "send_voice", "cancel_voice",
    "live_voice_config", "live_voice_tool", "live_voice_turn",
    "announce_worker", "record_worker_result", "resolve_worker_permission",
    "tts_status", "tts_voices", "preview_voice", "stt_status",
    "transcribe_audio",
]
GITHUB_BRIDGE = [
    "github_env", "github_set_token", "github_forget_token", "github_list_repos",
    "github_status", "github_clone", "github_create_and_open", "github_connect",
    "github_create_and_connect", "github_pull", "github_sync", "github_disconnect",
    "github_open_pulls", "github_review_pr", "github_address_pr",
    "github_setup_phone_access",
]
MIXINS = [(devices_api.DeviceApi, DEVICE_BRIDGE), (voice_api.VoiceApi, VOICE_BRIDGE),
          (github_api.GitHubApi, GITHUB_BRIDGE)]
BRIDGE = DEVICE_BRIDGE + VOICE_BRIDGE + GITHUB_BRIDGE


def test_every_moved_method_is_still_callable_on_api():
    """The whole risk of this refactor, in one assertion."""
    missing = [name for name in BRIDGE if not callable(getattr(gui_app.Api, name, None))]
    assert not missing, f"the frontend calls these and they are gone: {missing}"


def test_they_are_inherited_rather_than_redefined():
    """If a copy were left behind in app.py, the two would drift and the one
    that wins would depend on the MRO -- which nobody reads."""
    for mixin, names in MIXINS:
        for name in names:
            assert name in vars(mixin), f"{name} is not on {mixin.__name__}"
            assert name not in vars(gui_app.Api), f"{name} was left behind in Api too"


def test_the_mixins_are_actually_mixed_in():
    for mixin, _ in MIXINS:
        assert mixin in gui_app.Api.__mro__


def test_pywebview_would_still_see_them():
    """It exposes public methods of the instance by inspection. Inherited ones
    are found the same as defined ones; a collaborator OBJECT would not be, and
    would break only in the running app."""
    api = gui_app.Api.__new__(gui_app.Api)
    exposed = {n for n in dir(api) if not n.startswith("_") and callable(getattr(api, n, None))}
    assert set(BRIDGE) <= exposed


def test_the_private_helpers_moved_with_the_methods_that_use_them():
    """A helper left behind would still work, and would be the seam starting to
    leak back."""
    for name in ("_open_sync_store", "_notify_phone", "_finish_one_interrupted",
                 "_finish_picked_up_turn", "webpush_keys"):
        assert name in vars(devices_api.DeviceApi), f"{name} did not move"
    for name in ("_voice_sid", "_ensure_convo", "_prewarm_speech", "_ack_audio",
                 "_run_convo_turn", "_persist_voice_turn", "_record_voice_exchange",
                 "_append_voice_messages", "_drain_voice_turns", "_last_convo_reply",
                 "_queue_worker_report", "_drain_worker_reports"):
        assert name in vars(voice_api.VoiceApi), f"{name} did not move"
    for name in ("_clone_root", "_gh_token", "_active_repo_coords",
                 "_format_pr_comments"):
        assert name in vars(github_api.GitHubApi), f"{name} did not move"


def test_app_py_actually_got_smaller():
    """Otherwise this was a rename with extra steps. 190 before the first seam,
    171 after DeviceApi, 143 after VoiceApi, 121 after GitHubApi (this count
    includes properties, which the method tally in the notes does not)."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    api = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Api")
    methods = [n for n in api.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(methods) < 130, (
        f"Api still defines {len(methods)} methods; the split did not take")


def test_nothing_on_a_mixin_reaches_for_a_global():
    """These methods use self for everything (self._cfg, self._chats,
    self._store). One that quietly reached into app.py's module scope instead
    would work today and break the next time a seam is cut -- and a lazy
    `from .app import X` inside a function is the same leak wearing a hat,
    since it only exists to dodge the import cycle the seam is meant to end."""
    for name in ("devices_api.py", "voice_api.py", "github_api.py"):
        src = (GUI / name).read_text(encoding="utf-8")
        for forbidden in ("_chats[", "from .app import", "import app"):
            assert forbidden not in src.replace("self._chats[", ""), f"{name}: {forbidden}"


def test_the_event_sink_moved_out_of_the_api_module_and_is_still_reachable():
    """WebEvents is not part of the Api class and never was -- it is the other
    side of the bridge. The voice seam is what forced the issue: a mixin in its
    own module cannot construct a class defined in app.py.

    app.py re-exports it, which is why every existing WebEvents test still
    imports it from there and still passes. That is the proof the move changed
    where the code lives and nothing about what it does."""
    assert gui_app.WebEvents is not None
    assert gui_app.WebEvents.__module__ == "glmcode.gui.events"
    assert gui_app._TtsFeeder.__module__ == "glmcode.gui.events"
    # Same for the two leaf helpers both sides need.
    assert gui_app._tts_engine_voice.__module__ == "glmcode.gui.speech"
    assert gui_app._data_uri.__module__ == "glmcode.gui.media"


def test_the_leaves_stay_leaves():
    """speech.py and media.py exist so that events.py and voice_api.py can each
    have what they need without importing the other. An import back up the
    stack would recreate exactly the cycle they were carved out to break."""
    for name in ("speech.py", "media.py", "paths.py"):
        src = (GUI / name).read_text(encoding="utf-8")
        for forbidden in ("from .app", "from .events", "from .voice_api",
                          "from .devices_api", "from .github_api"):
            assert forbidden not in src, f"{name} imports {forbidden}"


def test_the_mixins_may_lean_on_each_other_through_self():
    """Not every GitHub call belongs to the GitHub seam. Chat sync, pairing and
    the CI runner also speak to GitHub, but their subject is reaching a machine
    that is not this one -- GitHub is the transport, not the point. So they
    stay in DeviceApi and reach `self._gh_token()`, which resolves through the
    MRO. That is a mixin working as intended, not a leak."""
    assert "_gh_token" in vars(github_api.GitHubApi)
    assert "_gh_token" not in vars(devices_api.DeviceApi)
    devices = (GUI / "devices_api.py").read_text(encoding="utf-8")
    assert "self._gh_token()" in devices
