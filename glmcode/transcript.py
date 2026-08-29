"""Append-only, per-chat conversation transcripts.

The session store persists the model's CURRENT context -- which means
compaction permanently shrinks what's on disk too. Transcripts fix that:
an append-only markdown log of everything ever said in a chat, living in
the global config dir (~/.makenomistakes/transcripts/, one file per chat). They
survive compaction, app restarts, and session switches -- and because
they're plain text files, the agent itself can grep/read them to recover
context that's no longer in its window, including from PAST chats.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR

TRANSCRIPTS_DIR = CONFIG_DIR / "transcripts"
MAX_TOOL_RESULT_CHARS = 1500


def _safe(sid: str) -> str:
    return "".join(ch for ch in sid if ch.isalnum() or ch in "-_")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


class Transcript:
    """Best-effort by design: a transcript write must never break a turn,
    so all filesystem errors are swallowed."""

    def __init__(self, session_id: str, cwd: str = "", root: Path | None = None):
        self.root = root or TRANSCRIPTS_DIR
        self.path = self.root / f"{_safe(session_id)}.md"
        self._cwd = cwd

    # ------------------------------------------------------------------ #

    def user(self, text: str, label: str = "User") -> None:
        text = (text or "").strip()
        if text:
            self._append(f"\n## {label} [{_now()}]\n{text}\n")

    def assistant(self, text: str, tool_calls: list | None = None) -> None:
        parts = []
        if text and text.strip():
            parts.append(text.strip())
        for tc in tool_calls or []:
            parts.append(f"-> tool call: {tc}")
        if parts:
            self._append(f"\n## Assistant [{_now()}]\n" + "\n".join(parts) + "\n")

    def tool_result(self, name: str, content: str, is_error: bool = False,
                    call_id: str = "") -> None:
        content = content or ""
        body = content[:MAX_TOOL_RESULT_CHARS]
        if len(content) > MAX_TOOL_RESULT_CHARS:
            body += f"\n... [truncated; full result was {len(content)} chars]"
        tag = "tool ERROR" if is_error else "tool result"
        self._append(f"\n### {tag}: {name}\n{body}\n")

    def marker(self, text: str) -> None:
        """An out-of-band note, e.g. 'context was compacted here'."""
        self._append(f"\n---\n*{text}*\n")

    def set_title(self, title: str) -> None:
        """Record the chat's (AI-generated) title so both the sidebar search
        and the model's own grepping can find this file by topic."""
        title = (title or "").strip()
        if title:
            self._append(f"\nTitle: {title}\n")

    # ------------------------------------------------------------------ #

    def prompt_note(self) -> str:
        """System-prompt blurb telling the model these files exist and how
        to use them."""
        return (
            f"\n\n# Conversation transcripts\n"
            f"Every chat's FULL conversation is appended to a markdown transcript "
            f"file -- including everything that has since been compacted out of "
            f"your context.\n"
            f"This chat's transcript: {self.path}\n"
            f"All chats' transcripts (one file per chat, past chats included): {self.root}\n"
            f"If the user refers to something from earlier that you no longer have "
            f"-- or from a previous chat entirely -- use grep/read_file on those "
            f"files to look it up instead of guessing or asking them to repeat it."
        )

    def delete(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------ #

    def _append(self, text: str) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as f:
                if is_new:
                    f.write(f"# Chat transcript\nProject: {self._cwd}\n"
                            f"Started: {_now()}\n")
                f.write(text)
        except OSError:
            pass


# --------------------------------------------------------------------- #
# Full-text search across every chat's transcript (sidebar search).

_SNIPPET_LEN = 150


def search_sessions(sessions: list, query: str, root: Path | None = None) -> list:
    """Filter a session list (as returned by SessionStore.list) to those
    whose title or transcript contains `query`, case-insensitively. Each hit
    gains a "snippet" key: the matching transcript line, trimmed around the
    match. Sessions from before transcripts existed still match by title."""
    root = root or TRANSCRIPTS_DIR
    q = (query or "").strip().lower()
    if not q:
        return sessions
    out = []
    for s in sessions:
        title = s.get("title", "")
        if q in title.lower():
            out.append({**s, "snippet": ""})  # title match needs no snippet
            continue
        path = root / f"{_safe(s.get('id', ''))}.md"
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        i = text.lower().find(q)
        if i < 0:
            continue
        start = text.rfind("\n", 0, i) + 1
        end = text.find("\n", i)
        line = text[start:end if end != -1 else len(text)].strip()
        if len(line) > _SNIPPET_LEN:
            # keep the match visible: trim around it, not just the line head
            at = max(0, (i - start) - _SNIPPET_LEN // 3)
            line = ("…" if at else "") + line[at:at + _SNIPPET_LEN] + "…"
        out.append({**s, "snippet": line})
    return out
