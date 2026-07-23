"""GitHub integration: clone a repo into a local folder, then keep it in sync
(pull / commit / push) as the agent works.

Security model (this is the whole point of the module):
  * The token lives ONLY in the OS keyring / encrypted store (see
    secretstore.py). It is never written to config.json.
  * It is never embedded in the git remote URL or .git/config -- the remote
    stays a clean https://github.com/<owner>/<repo>.git. If a plaintext-URL
    remote is ever detected, we scrub it.
  * It is never passed on a command line (argv is visible to other processes).
    Each network op injects it through a GIT_ASKPASS helper via a child-process
    environment variable only, with GIT_TERMINAL_PROMPT=0 so git can never fall
    back to an interactive prompt (which would hang a headless GUI).
  * It is never logged. git stderr is surfaced for diagnostics, but the token
    is not in the URL or args, so there is nothing to redact there; we still
    strip the env note defensively.
  * All git invocations use argument lists (never a shell string), and every
    user-supplied value (owner/repo/host/path) is validated before use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR
from .secretstore import encode_account, get_store
from .tools import NO_WINDOW_KWARGS

# GitHub only, for now -- an allowlist, not a denylist. Extra hosts (GH
# Enterprise) would be added here deliberately, never inferred from input.
ALLOWED_HOSTS = ("github.com",)

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")           # owner / repo segments
_ASKPASS_DIR = CONFIG_DIR / "git-askpass"


class GitHubError(Exception):
    """Any failure in a git or GitHub-API operation, with a user-safe message."""


def available() -> bool:
    return shutil.which("git") is not None


# --------------------------------------------------------------------- #
# Validation / parsing

def parse_repo(url: str) -> tuple[str, str, str]:
    """Accept the forms a user actually pastes and return (host, owner, repo):
        https://github.com/owner/repo(.git)
        git@github.com:owner/repo(.git)
        owner/repo            (assumes github.com)
    Raises GitHubError on anything that isn't a valid, allowlisted repo ref --
    which is also the injection guard: owner/repo are constrained to a safe
    character set before they ever reach a git command or a URL."""
    s = (url or "").strip()
    if not s:
        raise GitHubError("Enter a repository (owner/repo or a GitHub URL).")
    host = "github.com"
    owner = repo = ""
    m = re.match(r"^git@([^:]+):([^/]+)/(.+)$", s)
    if m:
        host, owner, repo = m.group(1), m.group(2), m.group(3)
    elif "://" in s:
        from urllib.parse import urlparse
        u = urlparse(s)
        host = (u.hostname or "").lower()
        parts = [p for p in u.path.split("/") if p]
        if len(parts) < 2:
            raise GitHubError(f"That URL doesn't look like a repository: {s}")
        owner, repo = parts[0], parts[1]
    else:
        parts = [p for p in s.split("/") if p]
        if len(parts) != 2:
            raise GitHubError("Use owner/repo, or a full GitHub URL.")
        owner, repo = parts

    repo = repo[:-4] if repo.endswith(".git") else repo
    host = host.lower()
    if host not in ALLOWED_HOSTS:
        raise GitHubError(f"Only {', '.join(ALLOWED_HOSTS)} is supported (got {host}).")
    if not (_NAME_RE.match(owner) and _NAME_RE.match(repo)):
        raise GitHubError("That owner/repo has characters we don't allow.")
    if owner in (".", "..") or repo in (".", ".."):
        raise GitHubError("That owner/repo isn't valid.")
    return host, owner, repo


def clean_remote_url(host: str, owner: str, repo: str) -> str:
    return f"https://{host}/{owner}/{repo}.git"


def target_dir(clone_root: Path, owner: str, repo: str) -> Path:
    """A collision-safe destination under clone_root. Prefers <repo>, then
    <owner>-<repo>, then numbered suffixes -- and never escapes clone_root."""
    root = Path(clone_root).expanduser().resolve()
    for name in (repo, f"{owner}-{repo}"):
        cand = (root / name)
        if _within(root, cand) and not cand.exists():
            return cand
    i = 2
    while True:
        cand = root / f"{owner}-{repo}-{i}"
        if _within(root, cand) and not cand.exists():
            return cand
        i += 1


def _within(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


# --------------------------------------------------------------------- #
# Credential injection (GIT_ASKPASS) -- token via env only, never argv/URL

def _askpass_script() -> Path:
    """Write (once) a tiny helper that git calls for username/password. It
    echoes values from the environment we hand ONLY to the git child process,
    so the token never appears in argv or on disk in cleartext."""
    _ASKPASS_DIR.mkdir(parents=True, exist_ok=True)
    helper = _ASKPASS_DIR / "askpass_helper.py"
    if not helper.exists():
        helper.write_text(
            "import os, sys\n"
            "p = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "sys.stdout.write(os.environ.get('MNM_GIT_USER', 'x-access-token')\n"
            "                 if 'Username' in p else os.environ.get('MNM_GIT_TOKEN', ''))\n",
            encoding="utf-8",
        )
    if os.name == "nt":
        wrapper = _ASKPASS_DIR / "askpass.cmd"
        if not wrapper.exists():
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\n',
                               encoding="utf-8")
        return wrapper
    wrapper = _ASKPASS_DIR / "askpass.sh"
    if not wrapper.exists():
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n',
                           encoding="utf-8")
        try:
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
        except OSError:
            pass
    return wrapper


def _git_env(token: str | None) -> dict:
    env = dict(os.environ)
    # Never let git prompt on a terminal (would hang the GUI).
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Drop any stale credential vars inherited from the parent environment so a
    # no-token call can't accidentally reuse someone else's askpass/token.
    for k in ("GIT_ASKPASS", "MNM_GIT_TOKEN", "MNM_GIT_USER"):
        env.pop(k, None)
    if token:
        env["GIT_ASKPASS"] = str(_askpass_script())
        env["MNM_GIT_TOKEN"] = token
        env["MNM_GIT_USER"] = "x-access-token"
    return env


def _run_git(args: list[str], cwd: Path | None = None, token: str | None = None,
             timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True, encoding="utf-8", errors="replace",
        env=_git_env(token), timeout=timeout, **NO_WINDOW_KWARGS,
    )


def _git_ok(args, cwd=None, token=None, timeout=300, what="git operation"):
    try:
        r = _run_git(args, cwd=cwd, token=token, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitHubError(f"{what} timed out.")
    except OSError as e:
        raise GitHubError(f"Could not run git: {e}")
    if r.returncode != 0:
        raise GitHubError(_friendly_git_error(r.stderr or r.stdout, what))
    return r.stdout.strip()


def _friendly_git_error(stderr: str, what: str) -> str:
    s = (stderr or "").strip()
    low = s.lower()
    if "authentication failed" in low or "could not read" in low or "403" in low:
        return ("GitHub rejected the credentials. Check that your token is valid "
                "and has access to this repository.")
    if "repository not found" in low or "404" in low:
        return "Repository not found (or the token can't see it)."
    if "conflict" in low or "non-fast-forward" in low or "rejected" in low:
        return ("The remote has changes yours don't. Pull first (the app tries a "
                "rebase automatically); resolve any conflicts, then sync again.")
    if "would be overwritten" in low or "unmerged" in low or "conflict" in low:
        return "Merge conflict — resolve the conflicting files, then sync again."
    # Keep it short and never echo an env dump; the token isn't in here anyway.
    first = s.splitlines()[0] if s else what + " failed"
    return f"{what} failed: {first[:300]}"


# --------------------------------------------------------------------- #
# Repo operations


@dataclass
class SyncStatus:
    connected: bool = False
    host: str = ""
    owner: str = ""
    repo: str = ""
    branch: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: bool = False
    remote_url: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        return d


def is_git_repo(path: Path) -> bool:
    return (Path(path) / ".git").exists()


def clone(host: str, owner: str, repo: str, dest: Path, token: str | None,
          on_status=None) -> Path:
    """Clone into `dest` (must not yet exist). Returns the path. The remote is
    left clean (no token in the URL)."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise GitHubError(f"{dest} already exists and isn't empty.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if on_status:
        on_status(f"cloning {owner}/{repo}…")
    url = clean_remote_url(host, owner, repo)
    _git_ok(["clone", url, str(dest)], token=token, timeout=600, what="Clone")
    return dest


def _current_branch(path: Path) -> str:
    r = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    b = r.stdout.strip()
    return "" if b in ("", "HEAD") else b


def _remote_url(path: Path) -> str:
    r = _run_git(["remote", "get-url", "origin"], cwd=path)
    return r.stdout.strip() if r.returncode == 0 else ""


def _scrub_remote(path: Path, host: str, owner: str, repo: str) -> None:
    """If origin ever carries an embedded credential (from a manual setup or an
    older version), rewrite it to the clean form so no token rests in config."""
    url = _remote_url(path)
    if "@" in url and "://" in url:  # e.g. https://user:token@github.com/...
        _run_git(["remote", "set-url", "origin", clean_remote_url(host, owner, repo)],
                 cwd=path)


def status(path: Path, host="", owner="", repo="") -> SyncStatus:
    """Best-effort sync status for the HUD. Local only -- no network."""
    p = Path(path)
    st = SyncStatus(host=host, owner=owner, repo=repo)
    if not is_git_repo(p):
        return st
    st.remote_url = _remote_url(p)
    st.connected = bool(st.remote_url)
    st.branch = _current_branch(p)
    porcelain = _run_git(["status", "--porcelain"], cwd=p)
    st.dirty = bool(porcelain.stdout.strip())
    if st.branch:
        counts = _run_git(
            ["rev-list", "--left-right", "--count", f"origin/{st.branch}...HEAD"],
            cwd=p)
        if counts.returncode == 0 and counts.stdout.strip():
            try:
                behind, ahead = counts.stdout.split()
                st.behind, st.ahead = int(behind), int(ahead)
            except ValueError:
                pass
    return st


def commit_all(path: Path, message: str) -> bool:
    """Stage everything and commit. Returns True if a commit was made, False if
    the tree was clean. Ensures an identity exists so commit never fails on a
    fresh machine."""
    p = Path(path)
    _ensure_identity(p)
    _git_ok(["add", "-A"], cwd=p, what="Stage changes")
    if not _run_git(["status", "--porcelain"], cwd=p).stdout.strip():
        return False
    msg = (message or "").strip() or "Update via Make No Mistakes"
    _git_ok(["commit", "-m", msg], cwd=p, what="Commit")
    return True


def _ensure_identity(path: Path) -> None:
    if not _run_git(["config", "user.email"], cwd=path).stdout.strip():
        _run_git(["config", "user.email", "agent@makenomistakes.local"], cwd=path)
    if not _run_git(["config", "user.name"], cwd=path).stdout.strip():
        _run_git(["config", "user.name", "Make No Mistakes"], cwd=path)


def pull(path: Path, token: str | None, on_status=None) -> str:
    """Rebase local commits on top of the remote. Requires a clean tree (the
    caller commits first). Returns a short human summary."""
    p = Path(path)
    branch = _current_branch(p)
    if not branch:
        raise GitHubError("This repo has no active branch to pull.")
    if on_status:
        on_status("pulling from GitHub…")
    out = _git_ok(["pull", "--rebase", "origin", branch], cwd=p, token=token,
                  what="Pull")
    if "up to date" in out.lower():
        return "Already up to date."
    return "Pulled the latest from GitHub."


def push(path: Path, token: str | None, set_upstream: bool = False,
         on_status=None) -> str:
    p = Path(path)
    branch = _current_branch(p)
    if not branch:
        raise GitHubError("This repo has no branch to push.")
    if on_status:
        on_status("pushing to GitHub…")
    args = ["push"]
    if set_upstream:
        args += ["-u"]
    args += ["origin", branch]
    _git_ok(args, cwd=p, token=token, what="Push")
    return "Pushed to GitHub."


def sync(path: Path, token: str | None, message: str = "", on_status=None) -> str:
    """The one-button flow: commit anything local, rebase on the remote, push.
    Never force-pushes; a rebase conflict surfaces as a clear error."""
    p = Path(path)
    if not is_git_repo(p):
        raise GitHubError("This folder isn't a git repository.")
    committed = commit_all(p, message)
    # Only rebase if there's an upstream to rebase onto.
    branch = _current_branch(p)
    has_upstream = _run_git(
        ["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], cwd=p
    ).returncode == 0 if branch else False
    if has_upstream:
        pull(p, token, on_status=on_status)
    push(p, token, set_upstream=not has_upstream, on_status=on_status)
    return "Synced." if committed else "Synced (nothing new to commit)."


def connect_existing(path: Path, host: str, owner: str, repo: str,
                     token: str | None, on_status=None) -> str:
    """Mid-session flow: attach an existing local folder to a GitHub repo
    (typically a brand-new / empty one) and push everything up. Initialises git
    if needed, sets a clean origin, and does the first sync."""
    p = Path(path)
    if not p.is_dir():
        raise GitHubError("That folder doesn't exist.")
    if on_status:
        on_status("connecting to GitHub…")
    if not is_git_repo(p):
        _git_ok(["init"], cwd=p, what="git init")
    # main as the default branch name if we're starting fresh.
    if not _current_branch(p):
        _run_git(["checkout", "-b", "main"], cwd=p)
    url = clean_remote_url(host, owner, repo)
    if _remote_url(p):
        _run_git(["remote", "set-url", "origin", url], cwd=p)
    else:
        _run_git(["remote", "add", "origin", url], cwd=p)
    # Bring down anything already on the remote (an empty repo has nothing;
    # a repo with a README merges cleanly) before pushing our content.
    _run_git(["fetch", "origin"], cwd=p, token=token)
    commit_all(p, "Initial import via Make No Mistakes")
    branch = _current_branch(p) or "main"
    remote_has_branch = _run_git(
        ["ls-remote", "--heads", "origin", branch], cwd=p, token=token
    ).stdout.strip()
    if remote_has_branch:
        # Reconcile unrelated histories (e.g. remote made with a README).
        _run_git(["pull", "--rebase", "origin", branch], cwd=p, token=token)
    push(p, token, set_upstream=True, on_status=on_status)
    return "Connected and synced to GitHub."


def disconnect(path: Path) -> None:
    """Forget the remote (keeps all local files). The token is dropped
    separately by the caller via forget_token()."""
    p = Path(path)
    if is_git_repo(p) and _remote_url(p):
        _run_git(["remote", "remove", "origin"], cwd=p)


# --------------------------------------------------------------------- #
# Token storage (delegates to the secure secretstore)

def _account(host: str) -> str:
    # One token per host/user is the common case; scope by host.
    return encode_account("github-token", host)


def save_token(host: str, token: str) -> None:
    get_store().set(_account(host), (token or "").strip())


def load_token(host: str = "github.com") -> str | None:
    tok = get_store().get(_account(host))
    return tok or None


def forget_token(host: str = "github.com") -> None:
    get_store().delete(_account(host))


def token_backend() -> str:
    return get_store().backend_name


def get_store_secure() -> bool:
    """True when tokens live in the OS credential store (the strong path)."""
    return get_store().is_secure


# --------------------------------------------------------------------- #
# GitHub REST API (token verification, repo listing, repo creation)

_API = "https://api.github.com"


def _api(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(_API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "make-no-mistakes")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GitHubError("GitHub rejected the token (check it hasn't expired "
                              "and has the right repository permissions).")
        if e.code == 404:
            raise GitHubError("Not found (the token may not have access).")
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        raise GitHubError(f"GitHub API error {e.code}{': ' + detail if detail else ''}.")
    except urllib.error.URLError as e:
        raise GitHubError(f"Could not reach GitHub: {e.reason}")


def verify_token(token: str) -> dict:
    """Return {'login', 'name'} for a valid token; raise otherwise."""
    me = _api("GET", "/user", token)
    return {"login": me.get("login", ""), "name": me.get("name") or me.get("login", "")}


def list_repos(token: str, limit: int = 100) -> list[dict]:
    """The user's repos (most recently pushed first), trimmed to what the UI
    needs."""
    out: list[dict] = []
    page = 1
    while len(out) < limit and page <= 5:
        batch = _api("GET",
                     f"/user/repos?per_page=50&page={page}&sort=pushed&affiliation=owner,collaborator",
                     token)
        if not isinstance(batch, list) or not batch:
            break
        for r in batch:
            out.append({
                "full_name": r.get("full_name", ""),
                "private": bool(r.get("private")),
                "default_branch": r.get("default_branch", "main"),
                "pushed_at": r.get("pushed_at", ""),
                "empty": (r.get("size", 0) == 0),
            })
        page += 1
    return out[:limit]


def create_repo(token: str, name: str, private: bool = True,
                description: str = "") -> dict:
    """Create a new repo under the authenticated user; return its coords."""
    if not _NAME_RE.match(name or ""):
        raise GitHubError("Repository names may use letters, numbers, . _ - only.")
    r = _api("POST", "/user/repos", token, {
        "name": name, "private": bool(private),
        "description": description[:300], "auto_init": False,
    })
    return {
        "full_name": r.get("full_name", ""),
        "owner": (r.get("owner") or {}).get("login", ""),
        "name": r.get("name", name),
        "clone_url": r.get("clone_url", ""),
        "default_branch": r.get("default_branch", "main"),
    }
