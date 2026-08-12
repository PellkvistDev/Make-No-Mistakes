"""Headless CI runner: trigger parsing, branch naming, and the end-to-end
run→commit→push→PR flow against a local bare repo with a fake agent (no GitHub,
no real model)."""

from pathlib import Path
import subprocess

import pytest

from glmcode import ci
from glmcode import githubsync as gh

needs_git = pytest.mark.skipif(not gh.available(), reason="git not installed")


# ------------------------------------------------------------- parsing --

def test_parse_task_extracts_after_trigger():
    assert ci.parse_task("/agent add a dark mode toggle") == "add a dark mode toggle"
    assert ci.parse_task("@bot /agent: fix the login bug") == "fix the login bug"
    assert ci.parse_task("please /agent do the thing\nand more") == "do the thing\nand more"


def test_parse_task_none_without_trigger():
    assert ci.parse_task("just a normal comment") is None
    assert ci.parse_task("") is None
    assert ci.parse_task("/agent   ") is None


def test_branch_name_is_slugged_and_unique():
    b1 = ci.branch_name("Add a Dark-Mode toggle!")
    b2 = ci.branch_name("Add a Dark-Mode toggle!")
    assert b1.startswith("agent/add-a-dark-mode-toggle-")
    assert b1 != b2                      # unique suffix


# --------------------------------------------------------- orchestration --

class _FakeAgent:
    """Stands in for the real agent: on run_turn it writes a file, mimicking a
    change, and records a 'final report' as its last assistant message."""

    def __init__(self, workdir, filename="feature.txt", body="new feature\n"):
        self.workdir = workdir
        self.filename = filename
        self.body = body
        self.messages = []

    def run_turn(self, msg):
        (self.workdir / self.filename).write_text(self.body, encoding="utf-8")
        self.messages = [msg, {"role": "assistant", "content": "Added the feature and a test."}]


def _clone_with_base(tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "-b", "main"], check=True, capture_output=True)
    (seed / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.email=a@b.c", "-c", "user.name=x",
                    "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    return bare, work


@needs_git
def test_run_ci_task_opens_pr_when_changed(tmp_path, monkeypatch):
    bare, work = _clone_with_base(tmp_path)
    # redirect origin to the local bare repo (push works without a token)
    monkeypatch.setattr(gh, "clean_remote_url", lambda h, o, r: str(bare))
    created = {}
    monkeypatch.setattr(gh, "create_pull",
                        lambda token, owner, repo, title, head, base, body="", draft=True:
                        created.update({"title": title, "head": head, "base": base})
                        or {"number": 12, "url": "http://x/pr/12"})
    comments = []
    monkeypatch.setattr(gh, "post_issue_comment",
                        lambda token, owner, repo, number, body: comments.append((number, body)) or "u")

    result = ci.run_ci_task("add a feature", workdir=work, owner="o", repo="r",
                            token=None, issue_number=5,
                            make_agent=lambda wd: _FakeAgent(wd), on_status=lambda *_: None)

    assert result["changed"] is True and result["pr"]["number"] == 12
    assert created["base"] == "main" and created["head"].startswith("agent/")
    # the change reached the bare remote on the agent branch
    got = subprocess.run(["git", "-C", str(bare), "branch", "--list", created["head"]],
                         capture_output=True, text=True).stdout
    assert created["head"] in got
    assert comments and comments[0][0] == 5 and "pr/12" in comments[0][1]


@needs_git
def test_run_ci_task_no_changes_comments_and_skips_pr(tmp_path, monkeypatch):
    bare, work = _clone_with_base(tmp_path)

    class _NoOpAgent:
        def __init__(self, wd): self.messages = [{"role": "assistant", "content": "Nothing needed."}]
        def run_turn(self, msg): pass

    pr_called = {"n": 0}
    monkeypatch.setattr(gh, "create_pull",
                        lambda *a, **k: pr_called.__setitem__("n", pr_called["n"] + 1) or {})
    comments = []
    monkeypatch.setattr(gh, "post_issue_comment",
                        lambda token, owner, repo, number, body: comments.append(body) or "u")

    result = ci.run_ci_task("do nothing", workdir=work, owner="o", repo="r", token=None,
                            issue_number=7, make_agent=lambda wd: _NoOpAgent(wd),
                            on_status=lambda *_: None)
    assert result["changed"] is False
    assert pr_called["n"] == 0
    assert comments and "didn't need to change" in comments[0]


# ------------------------------------------------ the workflow runs it all --

def test_every_mobile_test_file_is_actually_run_by_ci():
    """A suite CI never invokes is indistinguishable from one that passes.

    mobile/agent-core.test.js existed for a while before the workflow ran it,
    so every green check on every phone change was green without it. The
    workflow lists the files by name rather than by glob -- a glob that stops
    matching fails the same silent way -- which only works if adding a file
    also adds it there.
    """
    root = Path(__file__).parent.parent
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    missing = [p.name for p in sorted((root / "mobile").glob("*.test.js"))
               if f"mobile/{p.name}" not in workflow]
    assert not missing, (
        "these test files are never run by CI; add them to the `node --test` "
        "line in .github/workflows/ci.yml:\n  " + "\n  ".join(missing))


def test_the_browser_job_installs_what_its_tests_need_to_run():
    """The same silent failure as above, one level down.

    tests/mobile_ui reaches glmcode.pairing through importorskip, so when the
    browser-ui job installed no `requests` -- a transitive import of that
    module -- the pairing tests SKIPPED, for months, while the job's own
    comment said they were what pinned the two halves of the wire format
    together. A skipped test and a passing one look identical in a green check,
    which is exactly why importorskip needs this backstop.

    Only packages the app declares are checked. Anything in requirements.txt is
    something a contributor already has, so needing it here costs nothing.
    """
    root = Path(__file__).parent.parent
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installs = [ln for ln in workflow.splitlines() if "pip install" in ln]
    assert len(installs) == 2, "expected one pip install per job"
    for pkg in ("requests", "cryptography", "segno"):
        missing = [ln.strip() for ln in installs if pkg not in ln]
        assert not missing, (
            f"{pkg} is imported by tests in both jobs; without it they skip "
            f"rather than fail:\n  " + "\n  ".join(missing))


# ------------------------------------------------- source files stay text --

def test_no_source_file_contains_a_literal_nul_byte():
    """A single NUL makes a file binary to grep, ripgrep and most editors, and
    it fails silently: the search returns nothing and looks like no match.

    glmcode/gui/web/app.js had two, used as placeholder delimiters in the
    markdown renderer. The same file already wrote them as \\u0000 escapes two
    lines further down -- identical at runtime, and the escape leaves the file
    searchable. Git did not notice either, because its binary check only reads
    the first 8000 bytes and these were past that.
    """
    import glmcode
    roots = [Path(glmcode.__file__).parent, Path(__file__).parent.parent / "mobile"]
    exts = {".py", ".js", ".mjs", ".cjs", ".css", ".html", ".json", ".md", ".webmanifest"}
    offenders = []
    for root in roots:
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if "__pycache__" in p.parts:
                continue
            data = p.read_bytes()
            if b"\x00" in data:
                offenders.append(f"{p}: {data.count(chr(0).encode())} NUL byte(s)")
    assert not offenders, (
        "source files must stay searchable; write \\u0000 rather than the byte:\n"
        + "\n".join(offenders))
