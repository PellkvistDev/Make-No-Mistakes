"""Frontend abstraction: the agent reports everything through an AgentEvents
sink, so the same core drives the terminal UI and the desktop app."""

from __future__ import annotations

from contextlib import contextmanager


class AgentEvents:
    """No-op base implementation. Frontends override what they render."""

    # -- assistant streaming ------------------------------------------- #
    def stream_start(self) -> None: ...
    def reasoning_delta(self, text: str) -> None: ...
    def content_delta(self, text: str) -> None: ...
    def stream_end(self) -> None: ...

    # -- tools ---------------------------------------------------------- #
    def tool_call(self, name: str, args: dict) -> None: ...
    def tool_result(self, name: str, content: str, is_error: bool = False) -> None: ...
    def todos(self, items: list[dict]) -> None: ...

    # -- messages / status ---------------------------------------------- #
    def info(self, msg: str) -> None: ...
    def warn(self, msg: str) -> None: ...
    def error(self, msg: str) -> None: ...

    @contextmanager
    def status(self, label: str):
        """Long-running background step (vision analysis, compaction)."""
        yield

    # -- permissions ------------------------------------------------------ #
    def ask_permission(self, title: str, preview: str, always_label: str | None = None):
        """Return 'y', 'a', or ('n', feedback). Base: deny (safe default)."""
        return ("n", "no frontend attached to approve this")

    # -- turn lifecycle --------------------------------------------------- #
    def turn_done(self, usage, context: int = 0) -> None: ...

    # -- context compaction ----------------------------------------------- #
    def compacted(self, summary: str) -> None:
        """The conversation was summarized; `summary` is the retained context."""

    # -- sub-agents ------------------------------------------------------- #
    def subagent(self, id: str, name: str, status: str,
                 mission: str = "", summary: str = "") -> None:
        """Progress for a parallel sub-agent. status: 'running' | 'done' | 'error'."""

    # -- images ------------------------------------------------------------ #
    def show_image(self, path: str, caption: str = "") -> None:
        """Display an image inline for the user. No-op unless a UI is attached."""
