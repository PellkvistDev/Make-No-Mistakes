"""Headless agent runner for GitHub Actions — "work from your phone".

You comment `/agent <task>` on an issue or PR from the GitHub mobile app; a
workflow (see docs/agent-workflow.yml) runs this on a GitHub runner, the agent
does the work on a new branch, opens a draft PR, and comments back a link. Your
computer never turns on, nothing on your machine is exposed, and the free model
key lives only as an encrypted Actions secret. The human gate is the merge:
work always lands as a PR you review — never auto-merged.

This module deliberately imports only the agent core (no GUI / pywebview), so it
runs on a bare runner with just `requests` installed. The orchestration is
factored so it's testable without GitHub or a real model (see tests).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

TRIGGER = "/agent"


def parse_task(comment_body: str) -> str | None:
    """Extract the task from a comment. Everything after the first `/agent`
    (which may follow an @-mention), across the rest of the comment. None if the
    trigger isn't present."""
    body = (comment_body or "").strip()
    low = body.lower()
    idx = low.find(TRIGGER)
    if idx == -1:
        return None
    task = body[idx + len(TRIGGER):].strip(" :\t\r\n")
    return task or None


def _slug(text: str, n: int = 6) -> str:
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    return "-".join(words[:n]) or "task"


def branch_name(task: str) -> str:
    return f"agent/{_slug(task)}-{uuid.uuid4().hex[:6]}"


def _run(args: list[str], cwd: Path) -> str:
    from .tools import NO_WINDOW_KWARGS
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          encoding="utf-8", errors="replace", **NO_WINDOW_KWARGS).stdout.strip()


def _current_branch(cwd: Path) -> str:
    return _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd) or "main"


class _CiEvents:
    """A no-op-ish event sink that just echoes progress to the Action log. In
    CI the permission mode is autonomous, so no prompts are needed."""

    def __getattr__(self, _name):
        def _noop(*a, **k):
            return None
        return _noop

    def info(self, msg):
        print(f"· {msg}", flush=True)

    def warn(self, msg):
        print(f"! {msg}", flush=True)

    def error(self, msg):
        print(f"✗ {msg}", flush=True)

    def content_delta(self, text):
        print(text, end="", flush=True)

    class _NullCtx:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def status(self, *a, **k):
        return self._NullCtx()

    def ask_permission(self, *a, **k):
        return "n"   # never asked in yolo mode, but deny by default just in case


def build_agent(workdir: Path):
    """A real autonomous agent for CI: free model, yolo permissions (the runner
    is an ephemeral sandbox and the output is a reviewed PR)."""
    from .agent import Agent
    from .api import ZaiClient
    from .config import load_config
    cfg = load_config()
    cfg.mode = "yolo"
    client = ZaiClient(cfg.resolve_api_key(), cfg.base_url)
    return Agent(cfg, client, events=_CiEvents(), workdir=Path(workdir))


def _report(agent) -> str:
    from .agent import _final_report_text
    return _final_report_text(agent.messages) or "(the agent left no written summary)"


def run_ci_task(task: str, *, workdir: Path, owner: str, repo: str, token: str,
                base_branch: str = "", issue_number: int | None = None,
                make_agent=build_agent, on_status=print) -> dict:
    """Run one task and land a PR. Returns {changed, pr?}. Git/GitHub calls go
    through githubsync so they're mockable in tests."""
    from . import githubsync as gh
    workdir = Path(workdir)
    base = base_branch or _current_branch(workdir)
    branch = branch_name(task)
    on_status(f"working on branch {branch} (base {base})")
    _run(["checkout", "-b", branch], workdir)

    agent = make_agent(workdir)
    agent.run_turn({"role": "user", "content": task})
    report = _report(agent)

    if not gh.commit_all(workdir, f"Agent: {task[:60]}"):
        msg = f"I looked into this but didn't need to change any files.\n\n{report}"
        if issue_number:
            try:
                gh.post_issue_comment(token, owner, repo, issue_number, msg)
            except Exception as e:
                on_status(f"could not comment: {e}")
        on_status("no changes; nothing to open a PR for")
        return {"changed": False, "report": report}

    gh.push(workdir, token, set_upstream=True)
    body = (f"Requested via `/agent` from an issue/PR comment.\n\n**Task:** {task}\n\n"
            f"---\n\n{report}\n\n"
            f"*Opened by the Make No Mistakes agent. Review before merging.*")
    pr = gh.create_pull(token, owner, repo, title=f"Agent: {task[:60]}",
                        head=branch, base=base, body=body, draft=True)
    if issue_number and pr.get("url"):
        try:
            gh.post_issue_comment(token, owner, repo, issue_number,
                                  f"Done — opened {pr['url']} for review.\n\n{report[:1500]}")
        except Exception as e:
            on_status(f"could not comment: {e}")
    on_status(f"opened PR {pr.get('url')}")
    return {"changed": True, "pr": pr, "report": report}


def main() -> int:
    task = parse_task(os.environ.get("MNM_TASK", ""))
    if not task:
        print("No `/agent` task found in the trigger; nothing to do.")
        return 0
    repo_full = os.environ.get("MNM_REPO", "")           # "owner/repo"
    owner, _, repo = repo_full.partition("/")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not (owner and repo and token):
        print("Missing MNM_REPO or GITHUB_TOKEN.", file=sys.stderr)
        return 1
    if not os.environ.get("ZAI_API_KEY"):
        print("Missing ZAI_API_KEY secret.", file=sys.stderr)
        return 1
    issue = os.environ.get("MNM_ISSUE")
    try:
        run_ci_task(task, workdir=Path.cwd(), owner=owner, repo=repo, token=token,
                    base_branch=os.environ.get("MNM_BASE", ""),
                    issue_number=int(issue) if issue else None)
    except Exception as e:  # never leave the Action hanging without a reason
        print(f"Agent run failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
