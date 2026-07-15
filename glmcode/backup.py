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
        return subprocess.run(
            ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.project_dir}", *args],
            capture_output=True, text=True, check=check,
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
