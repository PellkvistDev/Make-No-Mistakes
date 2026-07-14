"""Terminal UI for GLM Code, built on rich."""

from __future__ import annotations

import sys

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

console = Console(highlight=False)

ACCENT = "cyan"
DIM = "grey58"


def banner(model: str, vision_model: str, mode: str, cwd: str, version: str) -> None:
    title = Text()
    title.append(" GLM Code ", style=f"bold black on {ACCENT}")
    title.append(f" v{version}", style=DIM)
    console.print()
    console.print(title)
    console.print(Text(f"  model: {model}  |  vision: {vision_model}  |  mode: {mode}", style=DIM))
    console.print(Text(f"  cwd: {cwd}", style=DIM))
    console.print(Text("  /help for commands  |  Esc+Enter or Alt+Enter for newline", style=DIM))
    console.print()


def info(msg: str) -> None:
    console.print(Text(f"  {msg}", style=DIM))


def warn(msg: str) -> None:
    console.print(Text(f"  ! {msg}", style="yellow"))


def error(msg: str) -> None:
    console.print(Text(f"  x {msg}", style="bold red"))


# --------------------------------------------------------------------- #
# Streaming assistant output

class StreamRenderer:
    """Streams reasoning (dim) and content (markdown) live to the terminal."""

    def __init__(self, show_reasoning: bool = True):
        self.show_reasoning = show_reasoning
        self.reasoning = ""
        self.content = ""
        self._live: Live | None = None
        self._reasoning_done = False

    def __enter__(self):
        self._live = Live(
            console=console, refresh_per_second=10, vertical_overflow="visible"
        )
        self._live.__enter__()
        self._render()
        return self

    def __exit__(self, *exc):
        if self._live:
            self._render(final=True)
            self._live.__exit__(*exc)
            self._live = None
        return False

    def on_reasoning(self, chunk: str) -> None:
        self.reasoning += chunk
        self._render()

    def on_content(self, chunk: str) -> None:
        if self.reasoning and not self._reasoning_done:
            self._reasoning_done = True
        self.content += chunk
        self._render()

    def _render(self, final: bool = False) -> None:
        if not self._live:
            return
        parts = []
        if self.reasoning and self.show_reasoning:
            r = self.reasoning
            # while thinking, show only the tail to keep the screen calm
            if not self._reasoning_done and not final and len(r) > 600:
                r = "..." + r[-600:]
            label = "thinking" if not (self._reasoning_done or final) else "thought"
            parts.append(Text(f"* {label}:", style=f"italic {DIM}"))
            parts.append(Text(r.strip(), style=DIM))
        elif self.reasoning and not self._reasoning_done and not final:
            parts.append(Text("* thinking...", style=f"italic {DIM}"))
        if self.content:
            try:
                parts.append(Markdown(self.content))
            except Exception:
                parts.append(Text(self.content))
        if not parts:
            parts.append(Text("...", style=DIM))
        self._live.update(Group(*parts))


# --------------------------------------------------------------------- #
# Tool call rendering

def tool_call(name: str, args: dict) -> None:
    detail = _tool_summary(name, args)
    line = Text("  > ", style=ACCENT)
    line.append(name, style=f"bold {ACCENT}")
    if detail:
        line.append(f"  {detail}", style=DIM)
    console.print(line)


def _tool_summary(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file"):
        return str(args.get("path", ""))
    if name == "run_powershell":
        cmd = str(args.get("command", "")).replace("\n", " ")
        return cmd[:100] + ("..." if len(cmd) > 100 else "")
    if name == "grep":
        s = f"/{args.get('pattern', '')}/"
        if args.get("glob"):
            s += f" in {args['glob']}"
        return s
    if name == "glob":
        return str(args.get("pattern", ""))
    if name == "list_dir":
        return str(args.get("path", "."))
    if name == "fetch_url":
        return str(args.get("url", ""))
    if name == "web_search":
        return str(args.get("query", ""))
    if name == "todo_write":
        return f"{len(args.get('todos', []))} items"
    return ""


def tool_result(name: str, result: str, is_error: bool = False) -> None:
    if name == "todo_write":
        return  # todos get their own rendering
    first = result.strip().splitlines()[0] if result.strip() else "(empty)"
    if len(first) > 120:
        first = first[:120] + "..."
    style = "red" if is_error else DIM
    prefix = "    x " if is_error else "    . "
    console.print(Text(prefix + first, style=style))


def todos(items: list[dict]) -> None:
    if not items:
        return
    lines = []
    for t in items:
        status = t.get("status", "pending")
        mark = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}[status]
        style = {"completed": DIM, "in_progress": f"bold {ACCENT}", "pending": ""}[status]
        lines.append(Text(f"  {mark} {t['content']}", style=style))
    console.print(Panel(Group(*lines), title="todos", title_align="left",
                        border_style=DIM, padding=(0, 1)))


# --------------------------------------------------------------------- #
# Permission prompt

def ask_permission(title: str, preview: str, always_label: str | None = None):
    """Returns 'y', 'a', or ('n', feedback)."""
    body = _render_preview(preview)
    console.print(Panel(body, title=f"[bold yellow]{title}[/]", title_align="left",
                        border_style="yellow", padding=(0, 1)))
    opts = "[bold green]y[/]=yes  [bold red]n[/]=no"
    if always_label:
        opts += f"  [bold]a[/]={always_label}"
    console.print(f"  Allow? {opts}", highlight=False)
    while True:
        try:
            ans = console.input("  > ").strip().lower()
        except EOFError:
            return ("n", "")
        if ans in ("y", "yes", ""):
            return "y"
        if ans == "a" and always_label:
            return "a"
        if ans in ("n", "no"):
            fb = console.input(Text("  tell the model why (optional): ", style=DIM)).strip()
            return ("n", fb)
        console.print(Text("  please answer y, n" + (", or a" if always_label else ""), style=DIM))


def _render_preview(preview: str):
    if not preview:
        return Text("")
    if preview.startswith(("---", "+++", "NEW FILE")) or "\n@@" in preview or preview.startswith("@@"):
        txt = Text()
        for line in preview.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                txt.append(line + "\n", style="green")
            elif line.startswith("-") and not line.startswith("---"):
                txt.append(line + "\n", style="red")
            elif line.startswith("@@"):
                txt.append(line + "\n", style=ACCENT)
            else:
                txt.append(line + "\n", style=DIM if line.startswith(("---", "+++")) else "")
        return txt
    return Text(preview)


def spinner(label: str):
    return console.status(Text(label, style=DIM), spinner="dots")


def usage_line(prompt_tokens: int, completion_tokens: int, context_estimate: int) -> None:
    console.print(Text(
        f"  tokens: {prompt_tokens:,} in / {completion_tokens:,} out"
        f"  |  context ~{context_estimate:,}  |  cost: $0.00 (free tier)",
        style=DIM,
    ))


# --------------------------------------------------------------------- #
# AgentEvents implementation for the terminal

from contextlib import contextmanager

from .events import AgentEvents


class ConsoleEvents(AgentEvents):
    """Renders agent events with rich in the terminal (used by the CLI)."""

    def __init__(self, cfg=None):
        self.cfg = cfg
        self._renderer: StreamRenderer | None = None

    def stream_start(self) -> None:
        show = self.cfg.show_reasoning if self.cfg else True
        self._renderer = StreamRenderer(show_reasoning=show)
        self._renderer.__enter__()

    def reasoning_delta(self, text: str) -> None:
        if self._renderer:
            self._renderer.on_reasoning(text)

    def content_delta(self, text: str) -> None:
        if self._renderer:
            self._renderer.on_content(text)

    def stream_end(self) -> None:
        if self._renderer:
            self._renderer.__exit__(None, None, None)
            self._renderer = None

    def tool_call(self, name: str, args: dict) -> None:
        tool_call(name, args)

    def tool_result(self, name: str, content: str, is_error: bool = False) -> None:
        tool_result(name, content, is_error)

    def todos(self, items: list[dict]) -> None:
        todos(items)

    def info(self, msg: str) -> None:
        info(msg)

    def warn(self, msg: str) -> None:
        warn(msg)

    def error(self, msg: str) -> None:
        error(msg)

    @contextmanager
    def status(self, label: str):
        with spinner(label):
            yield

    def ask_permission(self, title: str, preview: str, always_label: str | None = None):
        return ask_permission(title, preview, always_label)

    def show_image(self, path: str, caption: str = "") -> None:
        info(f"[image] {path}" + (f" — {caption}" if caption else ""))
