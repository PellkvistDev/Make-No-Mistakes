"""One place that asks everything at once.

Every expensive bug in this app has had the same shape: something was off, or
half-configured, or quietly damaged, and NOTHING said so. A chat list that came
up empty. An extension reporting Connected while every command timed out. A
credential blob that would not decrypt, discovered by saving one credential and
losing the rest.

Each of those has a panel, and each panel is somewhere you only go once you
already suspect that thing. This asks all of them without being asked about any
of them.

The third rule is the one the feature exists for and the one worth testing
hardest: **a check that could not run reports `unknown`, never `ok`.**
Reporting "fine" for something untested is the mistake behind half of
CLAUDE.md, and it would be a poor thing for the diagnostic itself to make.
"""

import types

import pytest

import sys
sys.modules.setdefault("webview", types.SimpleNamespace(
    Window=object, FOLDER_DIALOG=object(), OPEN_DIALOG=object(), SAVE_DIALOG=object()))

from glmcode import config as config_mod        # noqa: E402
from glmcode import secretstore, syncstore      # noqa: E402
from glmcode.githubsync import GitHubError      # noqa: E402
from glmcode.gui import app as gui_app          # noqa: E402
from glmcode.gui import checkup_api            # noqa: E402


@pytest.fixture
def api(monkeypatch):
    """A machine where everything is fine. Each test breaks one thing."""
    a = gui_app.Api.__new__(gui_app.Api)
    a._cfg = config_mod.Config()
    a._cfg.browser_own = "off"
    a._cfg.browser_model = "a-big-model"
    a._cfg.providers = [{"name": "p", "base_url": "https://x/v1", "api_key": "k"}]
    # auto_backup and _backup_repo are properties reading the ACTIVE chat, so
    # the fixture needs a chat for them to read rather than plain attributes.
    a.session_id = "s1"
    a._chats = {"s1": types.SimpleNamespace(
        auto_backup=True,
        backup_repo=types.SimpleNamespace(list_snapshots=lambda: [1, 2, 3]))}
    a._gh_token = lambda: "T"
    a.sync_env = lambda: {"available": True, "enabled": False}
    a._open_sync_store = lambda: (None, "not used")
    a._active_repo_coords = lambda: None
    monkeypatch.setattr(checkup_api.githubsync, "available", lambda: True)

    # Pinned, not inherited from the machine running the tests: whether THIS
    # box has an OS keyring is not what any of these assertions are about.
    class Healthy:
        is_secure = True
        backend_name = "keyring"

        def read(self, account):
            return None
    monkeypatch.setattr(secretstore, "get_store", lambda: Healthy())
    return a


def _by_id(res):
    return {c["id"]: c for c in res["checks"]}


def test_a_healthy_machine_reports_no_problems(api):
    res = api.self_check()
    assert res["worst"] == "ok"
    assert res["problems"] == 0
    assert _by_id(res)["git"]["status"] == "ok"


def test_problems_come_first(api, monkeypatch):
    """The list is read top-down and the point is what to fix."""
    monkeypatch.setattr(checkup_api.githubsync, "available", lambda: False)
    api._cfg.browser_model = ""
    res = api.self_check()
    assert res["checks"][0]["id"] == "git"        # fail outranks warn
    assert res["worst"] == "fail"


def test_every_problem_says_what_to_do_about_it(api, monkeypatch):
    """A check that only names what is wrong is a dead end."""
    monkeypatch.setattr(checkup_api.githubsync, "available", lambda: False)
    api._cfg.browser_model = ""
    api._cfg.providers = []
    for c in api.self_check()["checks"]:
        if c["status"] in ("fail", "warn"):
            assert c["fix"].strip(), f"{c['id']} says what is wrong and not what to do"


# ---- rule 3: could not run is not the same as fine ----------------------

def test_a_store_it_could_not_reach_is_unknown_not_ok(api):
    api.sync_env = lambda: {"available": True, "enabled": True}
    api._open_sync_store = lambda: (None, "Could not reach GitHub: timed out")
    got = _by_id(api.self_check())["sync"]
    assert got["status"] == "unknown"
    assert "chats" in got["fix"].lower() or "online" in got["fix"].lower()


def test_a_damaged_index_is_a_failure_with_a_way_out(api):
    """Distinct from unreachable on purpose: collapsing those two is what cost
    the chat list in the first place."""
    store = types.SimpleNamespace(
        list=lambda: (_ for _ in ()).throw(syncstore.SyncError("index unreadable")))
    api.sync_env = lambda: {"available": True, "enabled": True}
    api._open_sync_store = lambda: (store, None)
    got = _by_id(api.self_check())["sync"]
    assert got["status"] == "fail"
    assert "rebuild" in got["fix"].lower()


def test_being_offline_is_still_not_a_verdict_on_your_data(api):
    store = types.SimpleNamespace(
        list=lambda: (_ for _ in ()).throw(GitHubError("Could not reach GitHub", 0)))
    api.sync_env = lambda: {"available": True, "enabled": True}
    api._open_sync_store = lambda: (store, None)
    assert _by_id(api.self_check())["sync"]["status"] == "unknown"


def test_a_credential_store_that_will_not_open_is_a_failure(api, monkeypatch):
    """This is the one you otherwise find out about by losing the rest."""
    class Broken:
        is_secure = True
        backend_name = "encrypted-file"

        def read(self, account):
            raise secretstore.SecretsUnreadable("they could not be read")
    monkeypatch.setattr(secretstore, "get_store", lambda: Broken())
    got = _by_id(api.self_check())["credentials"]
    assert got["status"] == "fail"
    assert "safe" in got["fix"].lower()


def test_an_unexpected_error_becomes_that_row_and_stops_nothing(api, monkeypatch):
    """Rule 2. A diagnostic that dies half way through is worse than none,
    because the rows it did print look like the whole answer."""
    def boom():
        raise RuntimeError("something nobody predicted")
    monkeypatch.setattr(api, "_check_credentials", boom)
    res = api.self_check()
    ids = _by_id(res)
    assert ids["credentials"]["status"] == "unknown"
    assert "RuntimeError" in ids["credentials"]["detail"]
    assert {"git", "backups", "model", "sync"} <= set(ids), "it stopped early"


# ---- the individual things that fail quietly ---------------------------

def test_backups_off_is_flagged_because_undo_depends_on_them(api):
    api._chats['s1'].auto_backup = False
    got = _by_id(api.self_check())["backups"]
    assert got["status"] == "warn"
    assert "snapshot" in (got["detail"] + got["fix"]).lower()


def test_a_provider_with_no_key_fails_every_turn(api):
    api._cfg.providers = [{"name": "p", "base_url": "https://x/v1", "api_key": ""}]
    got = _by_id(api.self_check())["model"]
    assert got["status"] == "fail"
    assert "401" in got["fix"]


def test_one_provider_missing_a_key_is_only_a_warning(api):
    api._cfg.providers = [{"name": "p", "base_url": "https://x/v1", "api_key": "k"},
                          {"name": "q", "base_url": "https://y/v1", "api_key": ""}]
    got = _by_id(api.self_check())["model"]
    assert got["status"] == "warn" and "q" in got["detail"]


def test_no_browser_model_is_named_because_nothing_else_names_it(api):
    api._cfg.browser_model = ""
    got = _by_id(api.self_check())["browser_model"]
    assert got["status"] == "warn"
    assert "small model" in got["fix"]


def test_a_listening_port_with_nothing_on_it_is_not_ok(api, monkeypatch):
    """The state everyone lands in after turning it on and before installing."""
    api._cfg.browser_own = "auto"
    monkeypatch.setattr(gui_app, "browser_extension", None, raising=False)
    from glmcode import browser_extension
    monkeypatch.setattr(browser_extension, "status",
                        lambda cfg, listen=False: {"enabled": True, "port": 8391,
                                                   "connected": False, "browser": ""})
    got = _by_id(api.self_check())["extension"]
    assert got["status"] == "warn"
    assert "8391" in got["detail"]


def test_a_chat_with_no_github_repo_is_not_nagged_about_runners(api):
    assert "ci" not in _by_id(api.self_check())
