"""PR review: the GitHub PR API helpers (fake urlopen), the post_pr_comment
tool, PR-branch checkout against a local bare repo, and the review-task
prompt composition."""

import io
import json
import subprocess
import urllib.error

import pytest

from glmcode import githubsync as gh
from glmcode.prompts import PR_ADDRESS_TASK, PR_REVIEW_TASK

needs_git = pytest.mark.skipif(not gh.available(), reason="git not installed")


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _routed_urlopen(routes):
    """routes: list of (predicate(url)->bool, payload_or_text, is_text)."""
    def opener(req, timeout=0):
        url = req.full_url
        for pred, payload, is_text in routes:
            if pred(url):
                data = payload if is_text else json.dumps(payload)
                return _Resp(data.encode("utf-8"))
        raise AssertionError(f"unexpected URL {url}")
    return opener


def test_list_open_pulls(monkeypatch):
    rows = [{"number": 7, "title": "Add feature", "user": {"login": "octo"},
             "head": {"ref": "feat"}, "base": {"ref": "main"}, "draft": False}]
    monkeypatch.setattr(gh.urllib.request, "urlopen",
                        _routed_urlopen([(lambda u: "/pulls?" in u, rows, False)]))
    pulls = gh.list_open_pulls("t", "o", "r")
    assert pulls[0]["number"] == 7 and pulls[0]["head"] == "feat"


def test_get_pull_and_diff(monkeypatch):
    pr = {"number": 7, "title": "T", "body": "desc", "user": {"login": "octo"},
          "head": {"ref": "feat"}, "base": {"ref": "main"}, "state": "open",
          "html_url": "http://x/7"}
    monkeypatch.setattr(gh.urllib.request, "urlopen", _routed_urlopen([
        (lambda u: u.endswith("/pulls/7"), pr, False),
    ]))
    got = gh.get_pull("t", "o", "r", 7)
    assert got["title"] == "T" and got["head"] == "feat" and got["base"] == "main"

    monkeypatch.setattr(gh.urllib.request, "urlopen", _routed_urlopen([
        (lambda u: u.endswith("/pulls/7"), "--- a\n+++ b\n@@\n+added", True),
    ]))
    diff = gh.pull_diff("t", "o", "r", 7)
    assert "+added" in diff


def test_pull_review_comments_merges_inline_and_issue(monkeypatch):
    inline = [{"path": "a.py", "line": 3, "user": {"login": "rev"}, "body": "fix this"}]
    issue = [{"user": {"login": "rev2"}, "body": "overall lgtm but..."}]
    monkeypatch.setattr(gh.urllib.request, "urlopen", _routed_urlopen([
        (lambda u: "/pulls/7/comments" in u, inline, False),
        (lambda u: "/issues/7/comments" in u, issue, False),
    ]))
    cs = gh.pull_review_comments("t", "o", "r", 7)
    assert any(c["path"] == "a.py" and c["line"] == 3 for c in cs)
    assert any(c["path"] == "" and "lgtm" in c["body"] for c in cs)


def test_post_issue_comment(monkeypatch):
    monkeypatch.setattr(gh.urllib.request, "urlopen", _routed_urlopen([
        (lambda u: "/issues/7/comments" in u, {"html_url": "http://x/c1"}, False),
    ]))
    assert gh.post_issue_comment("t", "o", "r", 7, "nice") == "http://x/c1"


@needs_git
def test_fetch_pr_branch_checks_out_the_head(tmp_path):
    # A local bare "remote" with a PR ref refs/pull/7/head that we can fetch.
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "-b", "main"], check=True, capture_output=True)
    (seed / "f.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.email=a@b.c", "-c", "user.name=x",
                    "commit", "-m", "base"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main:refs/pull/7/head"],
                   check=True, capture_output=True)

    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(bare), str(work)], check=True, capture_output=True)
    branch = gh.fetch_pr_branch(work, None, 7, "feat")
    assert branch == "feat"
    cur = subprocess.run(["git", "-C", str(work), "rev-parse", "--abbrev-ref", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    assert cur == "feat" and (work / "f.txt").exists()


def test_post_pr_comment_tool(tmp_path, monkeypatch):
    import glmcode.tools as tools
    tools.set_workdir(tmp_path)

    class _St:
        remote_url = "https://github.com/o/r.git"
    monkeypatch.setattr(gh, "status", lambda p: _St())
    monkeypatch.setattr(gh, "load_token", lambda host="github.com": "tok")
    posted = {}
    monkeypatch.setattr(gh, "post_issue_comment",
                        lambda token, owner, repo, number, body: posted.update(
                            {"n": number, "body": body, "repo": f"{owner}/{repo}"}) or "http://x/c")
    out = tools.post_pr_comment(9, "Looks good, one nit on line 4.")
    assert posted["n"] == 9 and posted["repo"] == "o/r" and "line 4" in posted["body"]
    assert "PR #9" in out


def test_post_pr_comment_tool_requires_connection(tmp_path, monkeypatch):
    import glmcode.tools as tools
    tools.set_workdir(tmp_path)

    class _St:
        remote_url = ""
    monkeypatch.setattr(gh, "status", lambda p: _St())
    with pytest.raises(tools.ToolErrorBase):
        tools.post_pr_comment(1, "hi")


def test_review_task_composition():
    task = PR_REVIEW_TASK.format(number=7, title="Add retry", author="octo", head="feat",
                                 base="main", body="adds backoff", comments="(none yet)",
                                 diff="+ code")
    assert "#7" in task and "Add retry" in task and "+ code" in task
    addr = PR_ADDRESS_TASK.format(number=7, title="Add retry", comments="- fix line 3")
    assert "fix line 3" in addr
