"""The Api class is being taken apart along real seams, not at a line count.

gui/app.py had grown to a single Api class of 190 methods across ~3,600 lines,
and every new feature landed in it. Sync, pairing, Web Push and the CI runner
are all one subject -- talking to a machine that is not this one -- and they
share their whole vocabulary, so they came out first.

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

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "glmcode" / "gui" / "app.py"

# Every method the frontend reaches on this subject. Named rather than derived,
# because the point is that this exact list keeps working -- a rule computed
# from the code would move when the code did.
BRIDGE = [
    "sync_env", "sync_enable", "sync_recovery_code", "sync_set_passphrase",
    "sync_forget_passphrase", "sync_list_chats", "sync_finish_interrupted",
    "sync_pull_chat", "sync_push_chat", "sync_catch_up", "sync_delete_chat",
    "webpush_public_key", "ci_status", "ci_install", "ci_dispatch",
]


def test_every_moved_method_is_still_callable_on_api():
    """The whole risk of this refactor, in one assertion."""
    missing = [name for name in BRIDGE if not callable(getattr(gui_app.Api, name, None))]
    assert not missing, f"the frontend calls these and they are gone: {missing}"


def test_they_are_inherited_rather_than_redefined():
    """If a copy were left behind in app.py, the two would drift and the one
    that wins would depend on the MRO -- which nobody reads."""
    for name in BRIDGE:
        assert name in vars(devices_api.DeviceApi), f"{name} is not on the mixin"
        assert name not in vars(gui_app.Api), f"{name} was left behind in Api too"


def test_the_mixin_is_actually_mixed_in():
    assert devices_api.DeviceApi in gui_app.Api.__mro__


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


def test_app_py_actually_got_smaller():
    """Otherwise this was a rename with extra steps."""
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    api = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Api")
    methods = [n for n in api.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert len(methods) < 180, (
        f"Api still defines {len(methods)} methods; the split did not take")


def test_nothing_on_the_mixin_reaches_for_a_global():
    """These methods use self for everything (self._cfg, self._chats,
    self._store). One that quietly reached into app.py's module scope instead
    would work today and break the next time a seam is cut."""
    src = (ROOT / "glmcode" / "gui" / "devices_api.py").read_text(encoding="utf-8")
    for forbidden in ("_chats[", "from .app import", "import app"):
        assert forbidden not in src.replace("self._chats[", ""), forbidden
