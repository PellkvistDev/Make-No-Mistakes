"""GitHub sync backend: URL parsing/validation (the injection guard), the
credential-injection plumbing (token never in argv/URL), and the full
clone/commit/push/pull/sync/connect flow exercised against LOCAL bare repos
(no network, no real token). GitHub API error handling is tested with a fake
urlopen."""

import io
import json
import subprocess
import urllib.error

import pytest

from glmcode import githubsync as gh
from glmcode.secretstore import MemoryBackend, SecretStore, set_store

needs_git = pytest.mark.skipif(not gh.available(), reason="git not installed")


# ------------------------------------------------------------- parsing --

def test_parse_repo_forms():
    assert gh.parse_repo("owner/repo") == ("github.com", "owner", "repo")
    assert gh.parse_repo("https://github.com/o/r.git") == ("github.com", "o", "r")
    assert gh.parse_repo("git@github.com:o/r.git") == ("github.com", "o", "r")
    assert gh.parse_repo("https://github.com/o/r/") == ("github.com", "o", "r")


def test_parse_repo_rejects_other_hosts():
    with pytest.raises(gh.GitHubError):
        gh.parse_repo("https://gitlab.com/o/r")
    with pytest.raises(gh.GitHubError):
        gh.parse_repo("https://evil.example.com/o/r")


def test_parse_repo_rejects_injection_and_junk():
    for bad in ["o/r; rm -rf ~", "o/r && curl evil", "../../etc/passwd",
                "o/$(whoami)", "just-one-part", ""]:
        with pytest.raises(gh.GitHubError):
            gh.parse_repo(bad)


def test_clean_remote_url_has_no_credentials():
    url = gh.clean_remote_url("github.com", "o", "r")
    assert url == "https://github.com/o/r.git"
    assert "@" not in url and "token" not in url.lower()


# ---------------------------------------------------------- target_dir --

def test_target_dir_avoids_collisions_and_stays_in_root(tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    d1 = gh.target_dir(root, "acme", "widget")
    assert d1 == root / "widget"
    d1.mkdir()
    d2 = gh.target_dir(root, "acme", "widget")
    assert d2 == root / "acme-widget"          # first collision -> owner-repo
    d2.mkdir()
    d3 = gh.target_dir(root, "acme", "widget")
    assert d3 == root / "acme-widget-2"
    # never escapes the root
    assert str(d3.resolve()).startswith(str(root.resolve()))


# ----------------------------------------------- credential injection --

def test_git_env_injects_token_only_via_env(tmp_path):
    env = gh._git_env("ghp_SECRET")
    assert env["MNM_GIT_TOKEN"] == "ghp_SECRET"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"].endswith((".sh", ".cmd"))


def test_git_env_no_token_still_blocks_prompts():
    env = gh._git_env(None)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "MNM_GIT_TOKEN" not in env
    assert "GIT_ASKPASS" not in env


def test_askpass_helper_reads_env_not_args():
    script = gh._askpass_script()
    helper = script.parent / "askpass_helper.py"
    body = helper.read_text(encoding="utf-8")
    assert "MNM_GIT_TOKEN" in body and "MNM_GIT_USER" in body
    # the token comes from the environment, never a command-line argument
    assert "argv" in body and "os.environ" in body


# ------------------------------------------------ end-to-end via bare repo --

def _make_bare(tmp_path, name="remote.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True,
                   capture_output=True)
    return bare


def _point_origin_at(monkeypatch, bare):
    # clone()/connect_existing() build an https github URL; redirect that to a
    # local bare repo so the real git mechanics run without a network or token.
    monkeypatch.setattr(gh, "clean_remote_url", lambda h, o, r: str(bare))


def _commit(repo, msg):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=x",
                    "commit", "-m", msg], check=True, capture_output=True)


@needs_git
def test_clone_commit_push_pull_roundtrip(tmp_path, monkeypatch):
    bare = _make_bare(tmp_path)
    # seed the bare repo with an initial commit on main
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", str(bare), str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "checkout", "-b", "main"], check=True, capture_output=True)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _commit(seed, "init")
    subprocess.run(["git", "-C", str(seed), "push", "-u", "origin", "main"],
                   check=True, capture_output=True)

    _point_origin_at(monkeypatch, bare)
    dest = tmp_path / "work"
    gh.clone("github.com", "o", "r", dest, token=None)
    assert (dest / "README.md").read_text(encoding="utf-8") == "hello\n"

    # local edit -> commit_all -> status shows one ahead -> push clears it
    (dest / "new.txt").write_text("x\n", encoding="utf-8")
    assert gh.commit_all(dest, "add new.txt") is True
    st = gh.status(dest, "github.com", "o", "r")
    assert st.connected and st.ahead == 1 and st.dirty is False
    gh.push(dest, token=None)
    assert gh.status(dest, "github.com", "o", "r").ahead == 0

    # an outside change lands on the remote (a fresh clone on main); pull it in
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True)
    (other / "outside.txt").write_text("y\n", encoding="utf-8")
    _commit(other, "outside")
    subprocess.run(["git", "-C", str(other), "push"], check=True, capture_output=True)
    gh.pull(dest, token=None)
    assert (dest / "outside.txt").exists()


@needs_git
def test_commit_all_no_changes_returns_false(tmp_path, monkeypatch):
    bare = _make_bare(tmp_path)
    _point_origin_at(monkeypatch, bare)
    work = tmp_path / "w"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    (work / "a.txt").write_text("1", encoding="utf-8")
    assert gh.commit_all(work, "first") is True
    assert gh.commit_all(work, "again") is False    # nothing changed


@needs_git
def test_sync_is_one_button_commit_and_push(tmp_path, monkeypatch):
    bare = _make_bare(tmp_path)
    _point_origin_at(monkeypatch, bare)
    work = tmp_path / "proj"
    work.mkdir()
    (work / "app.py").write_text("print(1)\n", encoding="utf-8")
    # connect_existing wires origin + does the first push
    gh.connect_existing(work, "github.com", "o", "r", token=None)
    # a later change syncs up in one call
    (work / "app.py").write_text("print(2)\n", encoding="utf-8")
    msg = gh.sync(work, token=None, message="tweak")
    assert "Synced" in msg
    assert gh.status(work, "github.com", "o", "r").ahead == 0


@needs_git
def test_connect_existing_pushes_all_content_to_empty_repo(tmp_path, monkeypatch):
    bare = _make_bare(tmp_path)                     # empty remote
    _point_origin_at(monkeypatch, bare)
    work = tmp_path / "folder"
    work.mkdir()
    (work / "one.txt").write_text("1\n", encoding="utf-8")
    (work / "two.txt").write_text("2\n", encoding="utf-8")
    gh.connect_existing(work, "github.com", "o", "r", token=None)
    # verify the bare repo actually received both files
    check = tmp_path / "verify"
    subprocess.run(["git", "clone", str(bare), str(check)], check=True, capture_output=True)
    assert (check / "one.txt").exists() and (check / "two.txt").exists()


@needs_git
def test_disconnect_keeps_files_drops_remote(tmp_path, monkeypatch):
    bare = _make_bare(tmp_path)
    _point_origin_at(monkeypatch, bare)
    work = tmp_path / "proj"
    work.mkdir()
    (work / "keep.txt").write_text("keep\n", encoding="utf-8")
    gh.connect_existing(work, "github.com", "o", "r", token=None)
    gh.disconnect(work)
    assert (work / "keep.txt").exists()             # files stay
    assert gh.status(work, "github.com", "o", "r").connected is False


# ------------------------------------------------------- token storage --

def test_token_storage_roundtrip(monkeypatch):
    set_store(SecretStore(MemoryBackend()))
    gh.save_token("github.com", "ghp_abc")
    assert gh.load_token("github.com") == "ghp_abc"
    gh.forget_token("github.com")
    assert gh.load_token("github.com") is None


# ----------------------------------------------------- GitHub API (fake) --

class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_urlopen(payload, code=200):
    def opener(req, timeout=0):
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "err", {},
                                         io.BytesIO(json.dumps({"message": "no"}).encode()))
        return _Resp(json.dumps(payload).encode("utf-8"))
    return opener


def test_verify_token_ok(monkeypatch):
    monkeypatch.setattr(gh.urllib.request, "urlopen",
                        _fake_urlopen({"login": "octocat", "name": "Octo"}))
    assert gh.verify_token("t")["login"] == "octocat"


def test_verify_token_rejected(monkeypatch):
    monkeypatch.setattr(gh.urllib.request, "urlopen", _fake_urlopen(None, code=401))
    with pytest.raises(gh.GitHubError):
        gh.verify_token("bad")


def test_list_repos_shapes_rows(monkeypatch):
    page = [{"full_name": "o/a", "private": True, "default_branch": "main",
             "pushed_at": "2026-01-01", "size": 0}]
    calls = {"n": 0}
    def opener(req, timeout=0):
        calls["n"] += 1
        return _Resp(json.dumps(page if calls["n"] == 1 else []).encode())
    monkeypatch.setattr(gh.urllib.request, "urlopen", opener)
    rows = gh.list_repos("t")
    assert rows and rows[0]["full_name"] == "o/a" and rows[0]["empty"] is True


def test_create_repo_validates_name(monkeypatch):
    with pytest.raises(gh.GitHubError):
        gh.create_repo("t", "bad name!")


# ------------------------------------------------------------- security --

def test_parse_repo_rejects_dot_segments():
    for bad in ["../x", "x/..", ".././y", "https://github.com/../x"]:
        with pytest.raises(gh.GitHubError):
            gh.parse_repo(bad)


@needs_git
def test_token_never_lands_in_remote_url_or_config(tmp_path, monkeypatch):
    # Even after a full connect+sync with a token, the stored origin URL must be
    # the clean form -- the token lives only in the (mocked) env, never on disk.
    bare = _make_bare(tmp_path)
    real_clean = gh.clean_remote_url
    monkeypatch.setattr(gh, "clean_remote_url", lambda h, o, r: str(bare))
    work = tmp_path / "proj"
    work.mkdir()
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    gh.connect_existing(work, "github.com", "o", "r", token="ghp_TOPSECRET")
    cfg = (work / ".git" / "config").read_text(encoding="utf-8")
    assert "ghp_TOPSECRET" not in cfg
    assert "TOPSECRET" not in cfg


def test_askpass_helper_file_contains_no_secret():
    # The helper script is written to disk; it must read the token from the
    # environment, never embed one.
    helper = gh._askpass_script().parent / "askpass_helper.py"
    assert "ghp_" not in helper.read_text(encoding="utf-8")


def test_friendly_error_does_not_echo_env_or_token():
    msg = gh._friendly_git_error("fatal: Authentication failed for 'https://...'", "Push")
    assert "token" in msg.lower()          # explains, doesn't leak
    assert "MNM_GIT_TOKEN" not in msg
