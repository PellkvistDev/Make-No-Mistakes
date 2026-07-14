"""Permission engine: decides whether a tool call runs, asks the user when needed.

Modes:
  ask       read-only tools run freely; file writes and commands need approval
  autoedit  file writes auto-approved; commands and web fetches still ask
  yolo      everything auto-approved

"Always allow" answers are remembered for the session: per-tool for file tools,
per command-prefix (first word, e.g. `git`, `npm`) for PowerShell.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from .tools import READONLY_TOOLS, FILE_WRITE_TOOLS, NETWORK_TOOLS, GIT_TOOLS, TOOL_FUNCTIONS

# Module-level command alias registry
_COMMAND_ALIASES: dict = {}


@dataclass
class Decision:
    allowed: bool
    feedback: str = ""  # optional user guidance passed back to the model on deny


@dataclass
class PermissionEngine:
    mode: str = "ask"
    allowed_tools: set = field(default_factory=set)
    allowed_prefixes: set = field(default_factory=set)
    command_aliases: dict = field(default_factory=dict)  # composite command prefixes -> base prefix

    def check(self, name: str, args: dict, asker) -> Decision:
        """asker(prompt_lines, preview) -> 'y' | 'a' | 'n' | ('n', feedback)"""
        if name in READONLY_TOOLS:
            return Decision(True)
        if self.mode == "yolo":
            return Decision(True)
        if name in self.allowed_tools:
            return Decision(True)

        if name in FILE_WRITE_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            return self._ask_file(name, args, asker)

        if name == "run_powershell":
            command = str(args.get("command", ""))
            prefix = command_prefix(command)
            # Resolve aliases (e.g., "npm run dev" -> "npm")
            resolved_prefix = self.command_aliases.get(prefix, prefix)
            if resolved_prefix and resolved_prefix in self.allowed_prefixes:
                return Decision(True)
            return self._ask_command(command, prefix, asker)

        if name in NETWORK_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            if name == "fetch_url":
                detail = f"Fetch URL: {args.get('url', '?')}"
            elif name == "web_search":
                detail = f"Search the web for: {args.get('query', '?')}"
            elif name == "view_image":
                detail = f"Send image to the vision model: {args.get('path', '?')}"
                if args.get("question"):
                    detail += f"\nFocus: {args['question']}"
            else:
                detail = str(args)[:500]
            return self._ask_generic(name, detail, asker)

        if name in GIT_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            return self._ask_generic(name, str(args)[:500], asker)

        return self._ask_generic(name, str(args)[:500], asker)

    # ------------------------------------------------------------------ #

    def _ask_file(self, name: str, args: dict, asker) -> Decision:
        path = str(args.get("path", "?"))
        preview = build_diff_preview(name, args)
        title = f"{'Edit' if name == 'edit_file' else 'Write'} file: {path}"
        answer = asker(title, preview, always_label=f"always allow {name} this session")
        return self._to_decision(answer, name=name)

    def _ask_command(self, command: str, prefix: str, asker) -> Decision:
        always = f"always allow `{prefix} ...` this session" if prefix else None
        answer = asker("Run PowerShell command:", command, always_label=always)
        if _ans(answer) == "a" and prefix:
            self.allowed_prefixes.add(prefix)
            return Decision(True)
        return self._to_decision(answer)

    def _ask_generic(self, name: str, preview: str, asker) -> Decision:
        answer = asker(f"Tool: {name}", preview,
                       always_label=f"always allow {name} this session")
        return self._to_decision(answer, name=name)

    def _to_decision(self, answer, name: str = "") -> Decision:
        a = _ans(answer)
        if a == "a":
            if name:
                self.allowed_tools.add(name)
            return Decision(True)
        if a == "y":
            return Decision(True)
        feedback = answer[1] if isinstance(answer, tuple) and len(answer) > 1 else ""
        return Decision(False, feedback)


def _ans(answer) -> str:
    return answer[0] if isinstance(answer, tuple) else answer


def command_prefix(command: str) -> str:
    command = command.strip()
    if not command:
        return ""
    first = command.split()[0].lower()
    # composite commands aren't safely prefix-allowlistable
    if any(ch in command for ch in (";", "|", "&", "`n")) and first not in ("git",):
        return ""
    return first


def resolve_command_alias(command: str, aliases: dict) -> str:
    """Resolve a command to its base prefix using aliases (e.g., npm/yarn/pnpm -> npm)."""
    prefix = command_prefix(command)
    # Check module-level registry first, then the provided aliases dict
    return _COMMAND_ALIASES.get(prefix, aliases.get(prefix, prefix))


def add_command_aliases(aliases: dict) -> None:
    """Register command aliases for permission allowlisting.
    Example: {'npm': 'npm', 'yarn': 'npm', 'pnpm': 'npm'} allows all package managers.
    """
    # Build a lookup where composite commands map to their base
    alias_map = {}
    for composite, base in aliases.items():
        if composite != base:
            alias_map[composite] = base
    # Store in module-level registry
    for composite, base in alias_map.items():
        _COMMAND_ALIASES[composite] = base


def build_diff_preview(name: str, args: dict) -> str:
    """Unified diff preview for write_file / edit_file."""
    try:
        path = Path(str(args.get("path", ""))).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path

        if name == "write_file":
            new = str(args.get("content", ""))
            old = ""
            if path.exists():
                old = path.read_text(encoding="utf-8", errors="replace")
            if not old:
                lines = new.splitlines()
                head = "\n".join(lines[:60])
                more = f"\n... [{len(lines) - 60} more lines]" if len(lines) > 60 else ""
                return f"NEW FILE ({len(lines)} lines):\n{head}{more}"
            return _unified(old, new, path.name)

        if name == "edit_file":
            if not path.exists():
                return "(file does not exist)"
            old = path.read_text(encoding="utf-8", errors="replace")
            old_s = str(args.get("old_string", ""))
            new_s = str(args.get("new_string", ""))
            if args.get("replace_all"):
                new = old.replace(old_s, new_s)
            else:
                new = old.replace(old_s, new_s, 1)
            return _unified(old, new, path.name)
    except Exception as e:
        return f"(preview unavailable: {e})"
    return ""


def _unified(old: str, new: str, name: str) -> str:
    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=3,
    ))
    if not diff:
        return "(no changes)"
    if len(diff) > 120:
        diff = diff[:120] + [f"... [{len(diff) - 120} more diff lines]"]
    return "\n".join(diff)
