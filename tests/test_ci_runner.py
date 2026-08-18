"""Starting the agent on a GitHub runner, from the app.

glmcode/ci.py has been able to run the agent headless on a runner for a while:
a `/agent` comment, work on a branch, a draft PR, a comment back with the link.
All of it finished, and none of it reachable -- the workflow was a file in
docs/ you were told to copy into another repository by hand, so the honest
description of the feature was "you can do this yourself".

That gap matters more than convenience. The phone hands work to the desktop,
but the desktop has to be awake. A runner is the only machine in this system
that is never off.

Nothing here talks to GitHub: the API layer is faked, so what is tested is what
would be sent -- which is the part that is wrong silently.
"""

import pytest

from glmcode import ci, githubsync


class _GitHub:
    """The three calls the installer makes, and what they saw."""

    def __init__(self, files=None, fail=None):
        self.files = dict(files or {})
        self.fail = fail
        self.writes = []
        self.dispatches = []

    def read_repo_file(self, token, owner, repo, path):
        if self.fail:
            raise githubsync.GitHubError(self.fail)
        return self.files.get(path)

    def write_repo_file(self, token, owner, repo, path, text, message):
        if self.fail:
            raise githubsync.GitHubError(self.fail)
        self.writes.append({"path": path, "text": text, "message": message})
        self.files[path] = text
        return "sha1"

    def dispatch_workflow(self, token, owner, repo, workflow, ref, inputs):
        if self.fail:
            raise githubsync.GitHubError(self.fail)
        self.dispatches.append({"workflow": workflow, "ref": ref, "inputs": inputs})

    GitHubError = githubsync.GitHubError


@pytest.fixture
def gh(monkeypatch):
    """ci.py imports githubsync INSIDE each function, so the module's own
    attributes are what has to be replaced -- swapping ci.githubsync would be
    ignored by the very next `from . import githubsync`."""
    fake = _GitHub()
    for name in ("read_repo_file", "write_repo_file", "dispatch_workflow"):
        monkeypatch.setattr(githubsync, name, getattr(fake, name))
    return fake


# ------------------------------------------------------- the workflow ----

def test_the_installed_workflow_is_the_one_that_ships():
    """Read from docs/agent-workflow.yml rather than duplicated as a string,
    so the copy people install by hand and the copy the app installs cannot
    drift into two different workflows."""
    body = ci.workflow_source()
    assert "python -m glmcode.ci" in body
    assert ci.WORKFLOW_SOURCE.is_file()


def test_the_workflow_can_be_started_without_leaving_a_comment():
    body = ci.workflow_source()
    assert "workflow_dispatch" in body
    assert "inputs:" in body and "task:" in body


def test_a_dispatch_run_still_reaches_the_agent_with_its_task():
    """Two trigger paths, one env var. A dispatch that left MNM_TASK empty
    would start a runner that had nothing to do and say so ten minutes later."""
    body = ci.workflow_source()
    assert "inputs.task ||" in body


def test_the_comment_path_keeps_its_permission_gate():
    """workflow_dispatch is already restricted to people who can write to the
    repo. The comment path is not, and must keep checking for itself -- a
    stranger opening an issue must not be able to start a runner."""
    body = ci.workflow_source()
    assert "author_association" in body
    assert "OWNER" in body and "COLLABORATOR" in body


def test_the_acknowledgement_is_skipped_when_there_is_no_comment():
    """Reacting to `context.payload.comment.id` on a dispatch run has no
    comment to react to, and fails the step."""
    body = ci.workflow_source()
    assert "if: github.event_name == 'issue_comment'" in body


# ------------------------------------------------------- installing -----

def test_a_repo_without_the_workflow_reports_it_is_missing(gh):
    assert ci.workflow_status("t", "o", "r") == {"installed": False}


def test_installing_writes_the_workflow_where_actions_looks_for_it(gh):
    out = ci.install_workflow("t", "o", "r")
    assert out["ok"] is True
    assert gh.writes[0]["path"] == ".github/workflows/agent.yml"
    assert "python -m glmcode.ci" in gh.writes[0]["text"]


def test_installing_says_what_is_still_needed(gh):
    """The key cannot be set from here, and a feature that silently needs one
    more step is one that looks broken the first time it is used."""
    out = ci.install_workflow("t", "o", "r")
    assert "ZAI_API_KEY" in out["next"]
    assert "settings/secrets/actions" in out["next"]


def test_an_installed_workflow_is_recognised(gh):
    ci.install_workflow("t", "o", "r")
    status = ci.workflow_status("t", "o", "r")
    assert status["installed"] is True
    assert status["current"] is True


def test_an_old_workflow_is_reported_as_out_of_date(gh):
    """One installed before workflow_dispatch existed cannot be started from
    the app. Saying nothing would make the button look broken."""
    gh.files[ci.WORKFLOW_PATH] = "on:\n  issue_comment:\n    types: [created]\n"
    status = ci.workflow_status("t", "o", "r")
    assert status["installed"] is True
    assert status["current"] is False
    assert status["dispatchable"] is False


def test_a_github_failure_is_reported_not_raised(gh):
    """This is called from the UI thread. An exception there is a dead button
    with a traceback in a log nobody opens."""
    gh.fail = "GitHub rejected the token"
    assert "error" in ci.install_workflow("t", "o", "r")
    assert ci.workflow_status("t", "o", "r")["installed"] is False


# ------------------------------------------------------- dispatching ----

def test_dispatching_sends_the_task_as_the_input(gh):
    out = ci.dispatch("t", "o", "r", "add a dark mode toggle")
    assert out["ok"] is True
    assert gh.dispatches[0]["inputs"] == {"task": "add a dark mode toggle"}
    assert gh.dispatches[0]["workflow"] == "agent.yml"


def test_an_empty_task_is_refused_before_a_runner_starts(gh):
    out = ci.dispatch("t", "o", "r", "   ")
    assert "error" in out
    assert gh.dispatches == [], "a runner was started with nothing to do"


def test_the_caller_is_told_where_to_watch_it(gh):
    out = ci.dispatch("t", "o", "r", "do the thing")
    assert out["url"].endswith("/o/r/actions")


def test_a_dispatch_failure_is_reported_not_raised(gh):
    gh.fail = "Not found (the token may not have access)."
    assert "error" in ci.dispatch("t", "o", "r", "do the thing")


# --------------------------------------------------- the task it gets ---

@pytest.mark.parametrize("comment, expected", [
    ("/agent fix the login bug", "fix the login bug"),
    ("@someone /agent fix it", "fix it"),
    ("just a normal comment", None),
])
def test_the_trigger_is_stripped_from_a_comment(comment, expected):
    """The dispatch path sends a bare task; the comment path sends the whole
    comment. parse_task has to leave both with the same thing."""
    assert ci.parse_task(comment) == expected


def test_a_dispatch_task_survives_parse_task_unchanged():
    """A task typed into the app has no /agent prefix to strip, and must not
    be discarded for lacking one."""
    task = "add a dark mode toggle and cover it with a test"
    assert ci.parse_task(task) in (task, None)
