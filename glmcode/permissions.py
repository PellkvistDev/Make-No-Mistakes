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
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools import (READONLY_TOOLS, FILE_WRITE_TOOLS, NETWORK_TOOLS, GIT_TOOLS,
                    IMAGE_GEN_TOOLS, TTS_TOOLS, BROWSER_TOOLS, TOOL_FUNCTIONS)

# Module-level command alias registry
_COMMAND_ALIASES: dict = {}

# --------------------------------------------------------------------- #
# Read-only command detection.
#
# The agent constantly runs inspection commands (git status, ls, cat, grep,
# ...) that can't change anything on disk. Asking for those every time is pure
# friction, so in every mode EXCEPT "ask" we let a *provably* read-only command
# run unprompted. The bar for "provably" is deliberately high: anything that
# could redirect to a file, substitute a subcommand, chain into a stage we
# don't recognize, or pass a mutating subcommand/argument falls through to the
# normal prompt. When in doubt, we ask.

# Whole commands that only ever read state (lower-cased, path stripped).
_SAFE_COMMANDS = frozenset({
    # navigation / shell no-ops
    "ls", "dir", "pwd", "cd", "tree", "clear", "cls", "true", "false",
    # printing / reading files
    "echo", "printf", "cat", "bat", "type", "head", "tail", "more", "less",
    "nl", "tac", "rev", "wc", "od", "hexdump", "xxd", "strings",
    # text search / compare (all non-mutating in their bare forms)
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "diff", "comm",
    "cmp", "sort", "uniq", "cut", "column", "fold", "expand", "look",
    # info / environment
    "whoami", "hostname", "uname", "id", "groups", "date", "cal", "uptime",
    "env", "printenv", "history", "which", "where", "whereis", "command",
    "file", "stat", "du", "df", "basename", "dirname", "realpath", "readlink",
    "wc", "ps", "top", "free", "lsof", "ifconfig", "ipconfig", "arch",
    # PowerShell read-only cmdlets / aliases
    "get-content", "gc", "get-childitem", "gci", "get-item",
    "get-itemproperty", "get-location", "gl", "test-path", "resolve-path",
    "select-string", "sls", "get-command", "gcm", "get-help", "get-member",
    "get-process", "get-date", "get-history", "measure-object",
    "select-object", "where-object", "sort-object", "group-object",
    "format-list", "format-table", "out-string", "write-output", "write-host",
    "compare-object", "convertto-json", "convertfrom-json",
})

# Tools where only certain *subcommands* are read-only. The first non-flag
# argument must be in the set; everything else falls through to a prompt.
_SAFE_SUBCOMMANDS = {
    "npm": {"ls", "list", "view", "outdated", "why", "root", "prefix", "help"},
    "pnpm": {"ls", "list", "why", "outdated", "root"},
    "yarn": {"list", "why", "outdated"},
    "pip": {"list", "show", "freeze", "check", "help"},
    "pip3": {"list", "show", "freeze", "check", "help"},
    "docker": {"ps", "images", "version", "info", "inspect", "logs"},
    "kubectl": {"get", "describe", "version", "logs", "explain"},
    "cargo": {"tree", "metadata"},
    "dotnet": {"--list-sdks", "--list-runtimes", "--info", "--version"},
}

# git is special: a bunch of subcommands only read, and a few are read-only
# *only in their listing form* (no positional argument -- e.g. `git branch`
# lists, but `git branch foo` / `git branch -D foo` mutate).
_GIT_READONLY = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree",
    "cat-file", "blame", "describe", "shortlog", "for-each-ref", "grep",
    "rev-list", "merge-base", "name-rev", "count-objects", "whatchanged",
    "cherry", "help", "version", "show-ref", "symbolic-ref",
})
_GIT_LIST_ONLY = frozenset({"branch", "tag", "remote", "config", "reflog", "notes"})

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[|;]")
_FORBIDDEN_SEG = ("<", ">", "`", "&", "|", "$(", "${", "@(", "$(")


def _cmd_basename(tok: str) -> str:
    tok = tok.strip().strip('"').strip("'")
    tok = re.split(r"[\\/]", tok)[-1]
    return tok.lower()


def _segment_readonly(seg: str) -> bool:
    toks = seg.split()
    if not toks:
        return False
    cmd = _cmd_basename(toks[0])
    rest = toks[1:]
    # Universal safe form: `<tool> --version` / `<tool> --help` and nothing
    # else. Well-behaved tools print and exit, ignoring any real work.
    if len(rest) == 1 and rest[0].lower() in ("--version", "--help", "-version"):
        return True
    if cmd in _SAFE_COMMANDS:
        return True
    if cmd == "git":
        return _git_segment_readonly(rest)
    if cmd in _SAFE_SUBCOMMANDS:
        args = [t for t in rest if t]
        return bool(args) and args[0].lower() in _SAFE_SUBCOMMANDS[cmd]
    return False


def _git_segment_readonly(rest: list) -> bool:
    args = [t for t in rest if t]
    if not args:
        return False
    sub = args[0].lower()
    if sub in _GIT_READONLY:
        return True
    if sub in _GIT_LIST_ONLY:
        # read-only only when there's no positional argument (flags are fine)
        return all(t.startswith("-") for t in args[1:])
    return False


def is_readonly_command(command: str) -> bool:
    """True only when `command` provably just reads state (see module note).

    Conservative by design: any redirection, command substitution, unknown
    command, or mutating subcommand/argument makes this return False so the
    caller falls back to asking the user.
    """
    cmd = (command or "").strip()
    if not cmd or "\n" in cmd or "\r" in cmd:
        return False
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(cmd) if s.strip()]
    if not segments:
        return False
    for seg in segments:
        if any(tok in seg for tok in _FORBIDDEN_SEG):
            return False
        if not _segment_readonly(seg):
            return False
    return True


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
    # Plan mode: the current turn is exploration-only. Enforced here rather
    # than by prompt-asking-nicely -- a hard deny with corrective feedback,
    # regardless of ask/autoedit/yolo mode or session allowlists.
    plan_only: bool = False

    def check(self, name: str, args: dict, asker) -> Decision:
        """asker(prompt_lines, preview) -> 'y' | 'a' | 'n' | ('n', feedback)"""
        if name in READONLY_TOOLS:
            return Decision(True)
        if self.plan_only:
            return Decision(False, (
                "Plan mode is active: only read-only exploration tools are "
                "allowed this turn. Finish exploring and write the plan."))
        if self.mode == "yolo":
            return Decision(True)
        if name in self.allowed_tools:
            return Decision(True)

        if name in FILE_WRITE_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            return self._ask_file(name, args, asker)

        if name in ("run_powershell", "run_background"):
            command = str(args.get("command", ""))
            # Provably read-only inspection commands (git status, ls, cat,
            # grep, ...) run unprompted in every mode except "ask".
            if name == "run_powershell" and self.mode != "ask" \
                    and is_readonly_command(command):
                return Decision(True)
            prefix = command_prefix(command)
            # Resolve aliases (e.g., "npm run dev" -> "npm")
            resolved_prefix = self.command_aliases.get(prefix, prefix)
            if resolved_prefix and resolved_prefix in self.allowed_prefixes:
                return Decision(True)
            return self._ask_command(command, prefix, asker, background=(name == "run_background"))

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
            elif name == "package_info":
                detail = f"Look up {args.get('ecosystem', '?')} package: {args.get('name', '?')}"
            elif name == "show_http_cat":
                detail = f"Show http.cat image for status {args.get('status_code', '?')}"
            else:
                detail = str(args)[:500]
            return self._ask_generic(name, detail, asker)

        if name in GIT_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            return self._ask_generic(name, str(args)[:500], asker)

        if name in IMAGE_GEN_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            from .imagegen import packages_installed
            preview = (f"Prompt: {args.get('prompt', '?')}\n"
                      f"Save to: {args.get('path') or '(auto-named under generated/)'}")
            if not packages_installed():
                preview += ("\n\n(First use: installs ~1-2GB of local ML packages and "
                           "downloads the sd-turbo model (~1.7GB), one-time. Runs fully "
                           "offline after that.)")
            return self._ask_generic(name, preview, asker)

        if name in TTS_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            from .tts import ready as tts_ready
            preview = (f"Text: {args.get('text', '?')}\n"
                      f"Save to: {args.get('path') or '(auto-named under generated/)'}")
            if not tts_ready():
                preview += ("\n\n(First use: installs a small package (~50MB) and "
                           "downloads the Kokoro model (~300MB), one-time. Runs fully "
                           "offline after that.)")
            return self._ask_generic(name, preview, asker)

        if name in BROWSER_TOOLS:
            if self.mode == "autoedit":
                return Decision(True)
            from .browser import ready as browser_ready
            preview = f"URL: {args.get('url', '?')}"
            if not browser_ready():
                preview += ("\n\n(First use: installs Playwright and downloads Chromium "
                           "(~150-300MB), one-time. Runs fully offline after that, aside "
                           "from loading the page itself.)")
            return self._ask_generic(name, preview, asker)

        return self._ask_generic(name, str(args)[:500], asker)

    # ------------------------------------------------------------------ #

    def _ask_file(self, name: str, args: dict, asker) -> Decision:
        path = str(args.get("path", "?"))
        preview = build_diff_preview(name, args)
        title = f"{'Edit' if name == 'edit_file' else 'Write'} file: {path}"
        answer = asker(title, preview, always_label=f"always allow {name} this session")
        return self._to_decision(answer, name=name)

    def _ask_command(self, command: str, prefix: str, asker, background: bool = False) -> Decision:
        always = f"always allow `{prefix} ...` this session" if prefix else None
        title = "Run in background:" if background else "Run PowerShell command:"
        answer = asker(title, command, always_label=always)
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
