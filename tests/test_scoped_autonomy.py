"""Scoped autonomy: per-path permission rules override the mode for file
writes. A trusted (allow) path auto-approves even in 'ask'; a protected
(ask/deny) path prompts/blocks even in 'yolo'. Most protective rule wins."""

from glmcode.permissions import (PermissionEngine, _glob_matches,
                                 path_rule_action)


# ------------------------------------------------------------- globs --

def test_glob_double_star_spans_dirs():
    assert _glob_matches("src/a/b/c.py", "src/**")
    assert not _glob_matches("lib/a.py", "src/**")


def test_glob_single_star_stays_in_segment():
    assert _glob_matches("config/app.json", "config/*.json")
    assert not _glob_matches("config/nested/app.json", "config/*.json")


def test_bare_name_matches_at_any_depth():
    assert _glob_matches(".env", ".env")
    assert _glob_matches("services/api/.env", ".env")
    assert not _glob_matches("env.py", ".env")


def test_leading_double_star():
    assert _glob_matches("a/b/secret.key", "**/*.key")
    assert _glob_matches("secret.key", "**/*.key")


def test_trailing_slash_is_whole_dir():
    assert _glob_matches("migrations/0001.sql", "migrations/")
    assert not _glob_matches("migrationsx/0001.sql", "migrations/")


# ---------------------------------------------------- action resolution --

def test_no_rules_returns_none(tmp_path):
    assert path_rule_action("a.py", tmp_path, []) is None


def test_most_protective_rule_wins(tmp_path):
    rules = [
        {"glob": "**", "action": "allow"},        # everything allowed...
        {"glob": ".env", "action": "deny"},       # ...except .env, blocked
        {"glob": "src/**", "action": "ask"},
    ]
    assert path_rule_action(str(tmp_path / "src" / "app.py"), tmp_path, rules) == "ask"
    assert path_rule_action(str(tmp_path / ".env"), tmp_path, rules) == "deny"
    assert path_rule_action(str(tmp_path / "README.md"), tmp_path, rules) == "allow"


def test_invalid_rules_are_ignored(tmp_path):
    rules = ["nope", {"glob": "", "action": "deny"}, {"glob": "x", "action": "bogus"},
             {"glob": "a.py", "action": "allow"}]
    assert path_rule_action(str(tmp_path / "a.py"), tmp_path, rules) == "allow"


# ---------------------------------------------------- engine integration --

def _asker(answer):
    calls = []
    def ask(title, preview, always_label=None):
        calls.append({"title": title, "always_label": always_label})
        return answer
    ask.calls = calls
    return ask


def _write_args(tmp_path, name="f.py"):
    return {"path": str(tmp_path / name), "content": "x = 1\n"}


def test_allow_path_auto_approves_in_ask_mode(tmp_path):
    eng = PermissionEngine(mode="ask", workdir=tmp_path,
                           path_rules=[{"glob": "src/**", "action": "allow"}])
    ask = _asker("n")
    d = eng.check("write_file", _write_args(tmp_path, "src/app.py"), ask)
    assert d.allowed
    assert ask.calls == []                    # never prompted


def test_ask_path_prompts_even_in_yolo(tmp_path):
    eng = PermissionEngine(mode="yolo", workdir=tmp_path,
                           path_rules=[{"glob": ".env", "action": "ask"}])
    ask = _asker("y")
    d = eng.check("write_file", _write_args(tmp_path, ".env"), ask)
    assert d.allowed                          # user said yes...
    assert len(ask.calls) == 1                # ...but was asked, despite yolo
    assert ask.calls[0]["always_label"] is None   # no session-wide bypass offered


def test_ask_path_denied_when_user_declines(tmp_path):
    eng = PermissionEngine(mode="yolo", workdir=tmp_path,
                           path_rules=[{"glob": ".env", "action": "ask"}])
    d = eng.check("write_file", _write_args(tmp_path, ".env"), _asker(("n", "leave it")))
    assert not d.allowed and d.feedback == "leave it"


def test_deny_path_blocks_even_in_yolo(tmp_path):
    eng = PermissionEngine(mode="yolo", workdir=tmp_path,
                           path_rules=[{"glob": "secrets/**", "action": "deny"}])
    ask = _asker("y")
    d = eng.check("write_file", _write_args(tmp_path, "secrets/keys.txt"), ask)
    assert not d.allowed
    assert "protected path" in d.feedback
    assert ask.calls == []                    # blocked outright, not even asked


def test_unmatched_path_falls_through_to_mode(tmp_path):
    # A rule set that doesn't match this file: normal 'ask' behavior applies.
    eng = PermissionEngine(mode="ask", workdir=tmp_path,
                           path_rules=[{"glob": "src/**", "action": "allow"}])
    ask = _asker("y")
    eng.check("write_file", _write_args(tmp_path, "other.py"), ask)
    assert len(ask.calls) == 1                # prompted normally (ask mode, no rule)


def test_rules_do_not_touch_commands(tmp_path):
    # path_rules only gate file writes; a command still follows the mode.
    eng = PermissionEngine(mode="yolo", workdir=tmp_path,
                           path_rules=[{"glob": "**", "action": "deny"}])
    d = eng.check("run_powershell", {"command": "echo hi"}, _asker("n"))
    assert d.allowed                          # yolo runs the command; rule is write-only


# ---------------------------------------------------- config + agent wiring --

def test_config_round_trips_path_rules(tmp_path, monkeypatch):
    from glmcode import config as cfgmod
    f = tmp_path / "config.json"
    monkeypatch.setattr(cfgmod, "CONFIG_FILE", f)
    monkeypatch.setattr(cfgmod, "CONFIG_DIR", tmp_path)
    c = cfgmod.Config()
    c.path_rules = [{"glob": ".env", "action": "deny"}]
    cfgmod.save_config(c)
    assert cfgmod.load_config().path_rules == [{"glob": ".env", "action": "deny"}]


def test_agent_permission_engine_shares_the_cfg_rule_list(scripted_agent):
    # The engine must hold the SAME list object as cfg, so an in-place settings
    # update (c.path_rules[:] = ...) reaches every live chat.
    agent = scripted_agent()
    assert agent.permissions.path_rules is agent.cfg.path_rules
    agent.cfg.path_rules.append({"glob": ".env", "action": "deny"})
    assert agent.permissions.path_rules == [{"glob": ".env", "action": "deny"}]


def test_normalize_path_rules_cleans_ui_input(monkeypatch):
    import sys, types
    sys.modules.setdefault("webview", types.ModuleType("webview"))
    from glmcode.gui import app
    out = app._normalize_path_rules([
        {"glob": "src/**", "action": "allow"},
        {"glob": "", "action": "deny"},          # empty glob -> dropped
        {"glob": "x", "action": "bogus"},        # bad action -> dropped
        {"glob": ".env", "action": "ASK"},       # upper-cased -> normalized
        {"glob": "src/**", "action": "allow"},   # duplicate -> dropped
        "nope",                                  # not a dict -> dropped
    ])
    assert out == [{"glob": "src/**", "action": "allow"},
                   {"glob": ".env", "action": "ask"}]
