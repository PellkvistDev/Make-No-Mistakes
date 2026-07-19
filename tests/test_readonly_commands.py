"""Read-only inspection commands (git status, ls, cat, grep, ...) run without
a permission prompt in every mode except "ask". The classifier is deliberately
strict: anything that could mutate state -- redirection, substitution, an
unknown command, a mutating subcommand/argument -- must fall through to a
prompt."""

import pytest

from glmcode.permissions import PermissionEngine, is_readonly_command


# --- the classifier ----------------------------------------------------- #

SAFE = [
    "git status",
    "git status -sb",
    "git log --oneline -20",
    "git diff HEAD~1",
    "git show abc123",
    "git rev-parse HEAD",
    "git ls-files",
    "git branch",             # list
    "git branch -a",          # flags only
    "git branch --list -vv",
    "git tag -l",
    "git remote -v",
    "git config --list",
    "ls",
    "ls -la src/",
    "dir",
    "pwd",
    "cat README.md",
    "type package.json",
    "head -n 50 app.py",
    "tail -n 100 log.txt",
    "grep -rn TODO .",
    "rg --hidden pattern",
    "findstr /s foo *.cs",
    "Get-Content .\\file.txt",
    "get-childitem -recurse",
    "Test-Path ./x",
    "Select-String -Pattern foo -Path bar.txt",
    "git status && git log --oneline",     # every stage read-only
    "cat a.txt | grep foo | sort",         # pipeline of read-only stages
    "npm ls",
    "npm outdated",
    "pip list",
    "pip show requests",
    "docker ps",
    "kubectl get pods",
    "node --version",
    "python --help",
    "/usr/bin/git status",                 # path-prefixed
    "whoami",
]

UNSAFE = [
    # mutating commands
    "rm -rf build",
    "git commit -m x",
    "git push",
    "git checkout main",
    "git reset --hard",
    "git branch -D feature",               # positional -> delete
    "git tag v1.0",                        # positional -> create
    "git remote add origin url",
    "git config user.name Bob",            # positional -> set
    "git stash",                           # bare stash SAVES (mutates)
    "npm install",
    "npm run build",
    "pip install requests",
    "docker rm container",
    "kubectl delete pod x",
    "mkdir newdir",
    "mv a b",
    "cp a b",
    "touch newfile",
    # redirection writes a file even from a read-only producer
    "cat a.txt > b.txt",
    "echo hi > file",
    "ls >> listing.txt",
    "Get-Content x | Out-File y",
    "Get-ChildItem | Remove-Item",
    # command substitution / call operator / background could hide anything
    "cat $(rm -rf x)",
    "echo `rm x`",
    "ls & rm -rf x",
    "git status; rm -rf build",            # second stage mutates
    "grep foo . | xargs rm",               # pipes into a mutator
    # unknown / arbitrary-code runners
    "python script.py",
    "node server.js",
    "bash deploy.sh",
    "./configure",
    "sudo ls",
    "sed -i s/a/b/ file",                  # -i edits in place
    "",
    "   ",
]


@pytest.mark.parametrize("cmd", SAFE)
def test_safe_commands_are_readonly(cmd):
    assert is_readonly_command(cmd) is True, cmd


@pytest.mark.parametrize("cmd", UNSAFE)
def test_unsafe_commands_are_not_readonly(cmd):
    assert is_readonly_command(cmd) is False, cmd


# --- integration with the permission modes ------------------------------ #

def _never(*a, **k):
    raise AssertionError("asker should NOT be called for an auto-approved command")


def _deny(*a, **k):
    return ("n", "")


def test_autoedit_auto_approves_readonly_command():
    eng = PermissionEngine(mode="autoedit")
    d = eng.check("run_powershell", {"command": "git status"}, _never)
    assert d.allowed is True


def test_yolo_auto_approves_readonly_command():
    eng = PermissionEngine(mode="yolo")
    d = eng.check("run_powershell", {"command": "ls -la"}, _never)
    assert d.allowed is True


def test_ask_mode_still_prompts_for_readonly_command():
    # "all modes except ask" -- ask keeps asking, even for a read-only command.
    eng = PermissionEngine(mode="ask")
    asked = {}
    def asker(title, preview, always_label=None):
        asked["yes"] = True
        return ("n", "")
    d = eng.check("run_powershell", {"command": "git status"}, asker)
    assert asked.get("yes") is True
    assert d.allowed is False


def test_autoedit_still_prompts_for_mutating_command():
    eng = PermissionEngine(mode="autoedit")
    asked = {}
    def asker(title, preview, always_label=None):
        asked["yes"] = True
        return ("n", "")
    d = eng.check("run_powershell", {"command": "rm -rf build"}, asker)
    assert asked.get("yes") is True
    assert d.allowed is False


def test_plan_mode_allows_readonly_command():
    # Exploring the repo IS the point of planning, so a read-only command runs
    # unprompted in plan mode -- even when the underlying mode is "ask".
    eng = PermissionEngine(mode="ask", plan_only=True)
    d = eng.check("run_powershell", {"command": "git status"}, _never)
    assert d.allowed is True


def test_plan_mode_still_blocks_mutating_command():
    eng = PermissionEngine(mode="yolo", plan_only=True)  # yolo can't bypass it
    d = eng.check("run_powershell", {"command": "rm -rf build"}, _deny)
    assert d.allowed is False
    assert "Plan mode" in d.feedback


def test_readonly_shortcut_does_not_apply_to_run_background():
    # Backgrounding a read-only command is unusual; keep the normal prompt.
    eng = PermissionEngine(mode="autoedit")
    asked = {}
    def asker(title, preview, always_label=None):
        asked["yes"] = True
        return ("n", "")
    d = eng.check("run_background", {"command": "git status"}, asker)
    assert asked.get("yes") is True
    assert d.allowed is False
