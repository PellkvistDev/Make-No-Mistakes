"""Per-chat file backups via a hidden shadow git repo.

Each chat session gets its own git repo (git-dir) that tracks the project
folder (work-tree) as a separate history from any git repo the project
already has -- we never touch the user's real .git, branches, or commits.
Before each user turn a snapshot is committed, tagged with that message, so
"revert to here" is just resetting the work-tree back to that commit.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR

BACKUPS_DIR = CONFIG_DIR / "backups"

# Applied via the shadow repo's own info/exclude, never the project's real
# .gitignore -- keeps snapshots fast and small without touching anything the
# user has configured for their own repo. Excluding ".git/" is the important
# one: without it we'd snapshot the user's entire real git history as inert
# binary blobs on every turn.
DEFAULT_EXCLUDES = [
    ".git/", "node_modules/", "__pycache__/", "*.pyc",
    ".venv/", "venv/", ".next/", "dist/", "build/", ".DS_Store",
]


def available() -> bool:
    return shutil.which("git") is not None


@dataclass
class Snapshot:
    commit: str
    message: str
    timestamp: str


class BackupRepo:
    def __init__(self, session_id: str, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.git_dir = BACKUPS_DIR / session_id

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        # errors="replace", not strict text=True: diff output can contain
        # non-UTF-8 bytes (latin-1 sources, binary blobs) and a decode crash
        # here would break snapshot/diff/revert on perfectly normal projects.
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.project_dir}", *args],
            capture_output=True, encoding="utf-8", errors="replace", check=check,
        )

    def _initialized(self) -> bool:
        return (self.git_dir / "HEAD").exists()

    def ensure_init(self) -> None:
        if self._initialized():
            return
        self.git_dir.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q")
        self._run("config", "user.email", "backup@glmcode.local")
        self._run("config", "user.name", "Make No Mistakes backups")
        exclude_file = self.git_dir / "info" / "exclude"
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        exclude_file.write_text("\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8")

    def snapshot(self, message: str) -> str | None:
        """Commit the project dir's current state, tagged with `message`
        (the user's prompt that's about to run). Returns the commit hash,
        or None if git isn't available."""
        if not available():
            return None
        self.ensure_init()
        self._run("add", "-A")
        msg = (message or "").strip().replace("\n", " ")[:500] or "(empty message)"
        self._run("commit", "--allow-empty", "-q", "-m", msg)
        rev = self._run("rev-parse", "HEAD")
        return rev.stdout.strip()

    def list_snapshots(self) -> list[Snapshot]:
        if not available() or not self._initialized():
            return []
        result = self._run("log", "--pretty=format:%H%x1f%cI%x1f%s", "--reverse", check=False)
        if result.returncode != 0:
            return []
        out = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f", 2)
            if len(parts) != 3:
                continue
            h, ts, msg = parts
            out.append(Snapshot(commit=h, message=msg, timestamp=ts))
        return out

    def turn_diff(self, max_chars: int = 8000) -> str:
        """Everything that changed in the work-tree since the last snapshot
        (i.e. since the current turn started) as a git diff -- lets the agent
        self-review its own changes before reporting. Read-only apart from
        intent-to-add records in the shadow index (needed so NEW files show
        up in the diff; the next snapshot's `add -A` supersedes them)."""
        if not available():
            return "(git is not installed, so no change tracking is available)"
        if not self._initialized():
            return ("(no pre-turn snapshot exists yet in this chat -- nothing "
                    "to diff against)")
        try:
            self._run("add", "-A", "-N", check=False)
            stat = self._run("diff", "HEAD", "--stat", check=False).stdout
            patch = self._run("diff", "HEAD", check=False).stdout
        except Exception as e:
            return f"(could not compute the diff right now: {e})"
        if not patch.strip():
            return "No changes since the pre-turn snapshot."
        out = f"Changes since this turn's pre-turn snapshot:\n\n{stat}\n{patch}"
        if len(out) > max_chars:
            out = out[:max_chars] + f"\n... [diff truncated at {max_chars} chars]"
        return out

    def turn_changes(self, per_file_chars: int = 4000) -> list[dict]:
        """Structured version of turn_diff for the review UI: one entry per
        changed file since the pre-turn snapshot: {path, status, diff} where
        status is git's A(dded)/M(odified)/D(eleted)/R(enamed)."""
        if not available() or not self._initialized():
            return []
        try:
            self._run("add", "-A", "-N", check=False)
            names = self._run("diff", "HEAD", "--name-status", check=False).stdout
        except Exception:
            return []
        out = []
        for line in names.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0][:1], parts[-1]
            try:
                diff = self._run("diff", "HEAD", "--", path, check=False).stdout
            except Exception:
                diff = ""
            if len(diff) > per_file_chars:
                diff = diff[:per_file_chars] + "\n... [diff truncated]"
            out.append({"path": path, "status": status, "diff": diff})
        return out

    def revert_file(self, path: str) -> None:
        """Put ONE file back the way it was at the pre-turn snapshot:
        restore a modified/deleted file's snapshot content, or delete a file
        that didn't exist yet. Path is repo-relative and must stay inside
        the project directory."""
        if not available() or not self._initialized():
            raise RuntimeError("no snapshots exist for this chat")
        target = (self.project_dir / path).resolve()
        if not str(target).startswith(str(self.project_dir.resolve())):
            raise RuntimeError("path escapes the project directory")
        # Does the snapshot know this file? (exit 0 = tracked at HEAD)
        tracked = self._run("cat-file", "-e", f"HEAD:{path}", check=False).returncode == 0
        if tracked:
            self._run("checkout", "HEAD", "--", path)
        else:
            # Added this turn: reverting means removing it.
            self._run("rm", "--cached", "--ignore-unmatch", "-q", "--", path, check=False)
            try:
                target.unlink(missing_ok=True)
            except OSError as e:
                raise RuntimeError(f"could not remove {path}: {e}")

    def revert_to(self, commit: str) -> None:
        """Reset the project dir's actual files back to how they looked at
        `commit`. Does not touch the chat conversation -- only files."""
        if not available() or not self._initialized():
            raise RuntimeError("no backups exist for this chat yet")
        self._run("reset", "--hard", commit)
        # -e .git is redundant with info/exclude (git clean skips ignored
        # paths by default) but cheap insurance against ever touching the
        # project's own real git directory.
        self._run("clean", "-fd", "-e", ".git")
