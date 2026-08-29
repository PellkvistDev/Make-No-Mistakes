"""Everything the desktop does with a GitHub repository.

The third seam out of gui/app.py, and the same rule as the first two: a
subject with its own vocabulary rather than a line drawn at a convenient
length. Cloning and connecting a project, the token, the clone root, push and
pull, and reviewing a pull request are one subject -- they all speak
`githubsync` and they all ask the same two questions, "is this chat a repo"
and "do we have a token".

Note what is NOT here. Chat sync, pairing and the CI runner also touch GitHub,
and they live in devices_api.py, because their subject is reaching a machine
that is not this one -- GitHub is the transport they happen to use, not what
they are about. `sync_env` calling `self._gh_token()` across that line is the
mixins sharing one instance, which is exactly what a mixin is for.

A MIXIN, for the reason the others are: pywebview exposes the Api instance's
public methods by inspection, so an inherited method is found exactly like a
defined one, while a method moved onto a collaborator object would not be --
and would fail only at runtime, in the app, on the one path nobody re-tests.
"""

from __future__ import annotations

from pathlib import Path

from .. import githubsync
from .. import secretstore
from ..config import save_config
from ..sessions import new_id
from .paths import WHITEBOARD_DIR


class GitHubApi:
    """Clone, connect, sync and review. Mixed into Api."""

    def _clone_root(self) -> Path:
        """Where cloned repos land: the configured folder, or the default
        sibling of the app + whiteboard folders."""
        raw = (self._cfg.github_clone_root or "").strip()
        if raw:
            return Path(raw).expanduser()
        return WHITEBOARD_DIR.parent / "repos"

    def _gh_token(self) -> str | None:
        return githubsync.load_token("github.com")

    def github_env(self):
        """Everything the UI needs to render the GitHub controls: whether git is
        present, whether a token is stored (and how securely), and the current
        clone-root / auto-sync settings."""
        store_secure = githubsync.get_store_secure()
        login = self._cfg.extra.get("github_login", "")
        return {
            "available": githubsync.available(),
            "token_present": bool(self._gh_token()),
            "login": login,
            "backend": githubsync.token_backend(),
            "secure": store_secure,
            "clone_root": str(self._clone_root()),
            "auto_pull": bool(self._cfg.github_auto_pull),
            "auto_push": bool(self._cfg.github_auto_push),
        }

    def github_set_token(self, token: str):
        """Verify a token against the GitHub API, then store it securely. The
        raw token is never returned or written to config -- only the resolved
        login name (public) is cached for display."""
        token = (token or "").strip()
        if not token:
            return {"error": "Enter a token."}
        try:
            who = githubsync.verify_token(token)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        try:
            githubsync.save_token("github.com", token)
        except secretstore.SecretsUnreadable as e:
            return {"error": str(e)}
        self._cfg.extra["github_login"] = who.get("login", "")
        save_config(self._cfg)
        return {"ok": True, **self.github_env()}

    def github_forget_token(self):
        try:
            githubsync.forget_token("github.com")
        except secretstore.SecretsUnreadable as e:
            return {"error": str(e)}
        self._cfg.extra.pop("github_login", None)
        save_config(self._cfg)
        return {"ok": True, **self.github_env()}

    def github_list_repos(self):
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        try:
            return {"repos": githubsync.list_repos(token)}
        except githubsync.GitHubError as e:
            return {"error": str(e)}

    def github_status(self):
        """Live sync status of the ACTIVE session's folder (no network)."""
        cs = self._active
        if cs is None:
            return {"connected": False}
        path = Path(cs.agent.workdir)
        try:
            st = githubsync.status(path)
            if st.remote_url:
                try:
                    st.host, st.owner, st.repo = githubsync.parse_repo(st.remote_url)
                except githubsync.GitHubError:
                    pass
            d = st.as_dict()
        except Exception:
            d = {"connected": False}
        d["token_present"] = bool(self._gh_token())
        return d

    def github_clone(self, url: str, auto_backup: bool = True):
        """Clone a repo into the clone-root and open a new session in it."""
        if not githubsync.available():
            return {"error": "git isn't installed or on PATH."}
        try:
            host, owner, repo = githubsync.parse_repo(url)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        token = self._gh_token()
        dest = githubsync.target_dir(self._clone_root(), owner, repo)
        try:
            githubsync.clone(host, owner, repo, dest, token,
                             on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        res = self._activate_session(new_id(), [], str(dest), 0, 0, [],
                                     auto_backup=auto_backup)
        res["sessions"] = self.list_sessions()
        res["github"] = self.github_status()
        return res

    def github_create_and_open(self, name: str, private: bool = True,
                               auto_backup: bool = True):
        """Create a brand-new repo and open it as a NEW chat.

        This is the New-chat flow, so there's no folder to attach yet (that's
        github_create_and_connect). The repo is created with an initial commit
        so it has a branch, then cloned into the clone root like any other."""
        if not githubsync.available():
            return {"error": "git isn't installed or on PATH."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first (Settings → GitHub)."}
        name = (name or "").strip()
        if not name:
            return {"error": "Name the new repository."}
        try:
            created = githubsync.create_repo(token, name, private, auto_init=True)
        except githubsync.GitHubError as e:
            msg = str(e)
            if "not accessible" in msg or "403" in msg or "rejected the token" in msg:
                msg = ("Your token isn't allowed to create repositories. In its GitHub "
                       "settings give it Repository access: All repositories, and "
                       "Permissions → Administration: Read and write (keep Contents: "
                       "Read and write).")
            return {"error": msg}
        full = created.get("full_name") or f"{created.get('owner','')}/{name}"
        return self.github_clone(full, auto_backup=auto_backup)

    def github_connect(self, url: str):
        """Mid-session: attach the ACTIVE folder to an existing (often empty)
        repo and push everything up."""
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        try:
            host, owner, repo = githubsync.parse_repo(url)
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        token = self._gh_token()
        try:
            githubsync.connect_existing(Path(cs.agent.workdir), host, owner, repo,
                                        token, on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast("Connected to GitHub and synced.", "info")
        return {"ok": True, "github": self.github_status()}

    def github_create_and_connect(self, name: str, private: bool = True):
        """Create a brand-new repo under the user's account, then connect the
        active folder to it and sync -- the smooth 'push this to a new repo' flow."""
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        try:
            made = githubsync.create_repo(token, name, private=private)
            githubsync.connect_existing(
                Path(cs.agent.workdir), "github.com", made["owner"], made["name"],
                token, on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(f"Created {made['full_name']} and synced.", "info")
        return {"ok": True, "github": self.github_status()}

    def github_pull(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        path = Path(cs.agent.workdir)
        token = self._gh_token()
        try:
            # Commit local work first so a rebase pull never fails on a dirty
            # tree (nothing is lost; the user can review the commit).
            githubsync.commit_all(path, "Local changes before pull")
            msg = githubsync.pull(path, token,
                                  on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(msg, "info")
        return {"ok": True, "github": self.github_status()}

    def github_sync(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        token = self._gh_token()
        try:
            msg = githubsync.sync(Path(cs.agent.workdir), token,
                                  message=cs.title or "Update via Make No Mistakes",
                                  on_status=lambda m: self._events.toast(m, "info"))
        except githubsync.GitHubError as e:
            return {"error": str(e)}
        self._events.toast(msg, "info")
        return {"ok": True, "github": self.github_status()}

    def github_disconnect(self):
        cs = self._active
        if cs is None:
            return {"error": "Open a chat first."}
        try:
            githubsync.disconnect(Path(cs.agent.workdir))
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True, "github": self.github_status()}

    # -- PR review -------------------------------------------------------- #

    def _active_repo_coords(self):
        cs = self._active
        if cs is None:
            return None
        try:
            st = githubsync.status(Path(cs.agent.workdir))
            if not st.remote_url:
                return None
            host, owner, repo = githubsync.parse_repo(st.remote_url)
            return host, owner, repo, Path(cs.agent.workdir)
        except Exception:
            return None

    @staticmethod
    def _format_pr_comments(comments) -> str:
        lines = []
        for c in comments:
            loc = f"{c['path']}:{c['line']}" if c.get("path") else "(general)"
            body = (c.get("body") or "").strip()[:600]
            lines.append(f"- [{loc}] {c.get('author', '')}: {body}")
        return "\n".join(lines)

    def github_open_pulls(self):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, _ = coords
        try:
            return {"pulls": githubsync.list_open_pulls(token, owner, repo)}
        except githubsync.GitHubError as e:
            return {"error": str(e)}

    def github_review_pr(self, number):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, _ = coords
        try:
            pr = githubsync.get_pull(token, owner, repo, int(number))
            diff = githubsync.pull_diff(token, owner, repo, int(number))
            comments = githubsync.pull_review_comments(token, owner, repo, int(number))
        except (githubsync.GitHubError, ValueError) as e:
            return {"error": str(e)}
        from ..prompts import PR_REVIEW_TASK
        task = PR_REVIEW_TASK.format(
            number=pr["number"], title=pr["title"], author=pr["author"],
            head=pr["head"], base=pr["base"], body=(pr["body"] or "(no description)")[:2000],
            comments=self._format_pr_comments(comments) or "(none yet)", diff=diff)
        self.send(task)
        return {"ok": True}

    def github_address_pr(self, number):
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        token = self._gh_token()
        if not token:
            return {"error": "Connect a GitHub token first."}
        _, owner, repo, workdir = coords
        try:
            pr = githubsync.get_pull(token, owner, repo, int(number))
            githubsync.fetch_pr_branch(workdir, token, int(number), pr.get("head", ""))
            comments = githubsync.pull_review_comments(token, owner, repo, int(number))
        except (githubsync.GitHubError, ValueError) as e:
            return {"error": str(e)}
        from ..prompts import PR_ADDRESS_TASK
        task = PR_ADDRESS_TASK.format(
            number=pr["number"], title=pr["title"],
            comments=self._format_pr_comments(comments) or "(no review comments found)")
        self.send(task)
        return {"ok": True, "github": self.github_status()}

    def github_setup_phone_access(self):
        """Write the GitHub Actions workflow that lets you run the agent from
        your phone into the connected repo, and point the user at the secret
        page. They Sync it up, add the ZAI_API_KEY secret, and can then comment
        /agent from anywhere."""
        coords = self._active_repo_coords()
        if coords is None:
            return {"error": "This chat isn't a connected GitHub repository."}
        _, owner, repo, workdir = coords
        tmpl = Path(__file__).resolve().parents[2] / "docs" / "agent-workflow.yml"
        try:
            content = tmpl.read_text(encoding="utf-8")
        except OSError:
            return {"error": "Workflow template missing — copy docs/agent-workflow.yml manually."}
        try:
            dest = workdir / ".github" / "workflows" / "agent.yml"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        except OSError as e:
            return {"error": f"Couldn't write the workflow: {e}"}
        return {"ok": True, "path": ".github/workflows/agent.yml",
                "secrets_url": f"https://github.com/{owner}/{repo}/settings/secrets/actions/new"}
