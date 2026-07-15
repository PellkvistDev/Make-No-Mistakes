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

    # -- steering ----------------------------------------------------------- #
    def steered(self, text: str) -> None:
        """A steering message the user sent mid-turn was just injected into
        the conversation. No-op unless a UI is attached."""

    # -- context compaction ----------------------------------------------- #
    def compacted(self, summary: str) -> None:
        """The conversation was summarized; `summary` is the retained context."""

    # -- sub-agents ------------------------------------------------------- #
    def subagent(self, id: str, name: str, status: str,
                 mission: str = "", summary: str = "") -> None:
        """Progress for a parallel sub-agent. status: 'running' | 'done' | 'error'."""

    def subagent_stream(self, id: str, kind: str, **data) -> None:
        """A single granular live event from inside a sub-agent's own run,
        tagged with its id -- e.g. kind='reasoning'/'content' with a 'text'
        payload, 'tool_call' with 'name'/'args', 'tool_result' with
        'name'/'content'/'is_error', or 'stream_start'/'stream_end' with no
        payload. Lets a UI show a sub-agent's own thread live, not just the
        coarse start/done/error status from subagent()."""

    # -- images ------------------------------------------------------------ #
    def show_image(self, path: str, caption: str = "") -> None:
        """Display an image inline for the user. No-op unless a UI is attached."""

    # -- audio --------------------------------------------------------------- #
    def show_audio(self, path: str, caption: str = "") -> None:
        """Display a playable audio clip inline for the user. No-op unless a
        UI is attached."""
