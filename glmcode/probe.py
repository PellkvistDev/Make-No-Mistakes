"""Watch the program think.

The agent can read the code and it can run the tests. It has never once seen
what the program actually DID. `run_command` returns stdout — what the program
chose to say — and a failing test returns a traceback, which is a list of line
numbers with no values in it. So the model does what a person does with no
debugger: it adds print statements, runs again, reads, adds more. That is
three or four round trips on a tier metered at twenty requests a day, to
recover information the interpreter had all along.

`trace_run` runs a command with a tracer attached and hands back the call tree
with arguments and return values, plus the locals at every frame of whatever
exception ended it. "`parse()` returned None because it was handed an empty
string" replaces the entire print-debugging loop with one tool call.

How it attaches, and why this way: a `sitecustomize.py` is written into a temp
directory that is put on `PYTHONPATH`. CPython imports `sitecustomize` at
startup, before the program's own code, for EVERY python process — so this
works for `pytest`, for `python -m thing`, for a script, and for a subprocess
the command itself spawns, without the command needing to know. Nothing is
patched in the project and nothing is left behind.

The four things that keep it from being a liability:

  - **Scoped to the project's own files.** Tracing the standard library and
    site-packages produces megabytes per second and buries the twenty lines
    that matter. A trace of somebody else's code is not what anybody asked for.

  - **Bounded twice** — by events collected in the child, and again by
    characters when rendered. An uncapped tool result once ballooned a chat to
    ~1.5M tokens in this app, which is why `MAX_TOOL_OUTPUT` exists, and a
    tracer is the single easiest way to do it again.

  - **Values are redacted before they are rendered.** A trace through an
    authentication path otherwise puts live credentials into a chat that is
    then synced to GitHub. `scan_secrets`' own patterns do the work, so there
    is one definition of what a secret looks like.

  - **Repr is defensive.** `repr()` on a half-constructed object raises, on a
    lazy ORM row it makes a database call, and on a numpy array it can be
    megabytes. A tracer that crashes the program it is observing, or changes
    what the program does, is worse than no tracer.

Python only, and it says so rather than appearing to support everything: the
attach mechanism is `sitecustomize`, which is a CPython feature. Naming the
limit is the difference between a tool that does not apply and one that looks
broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from .tools import (MAX_TOOL_OUTPUT, NO_WINDOW_KWARGS, _truncate,
                    get_workdir)

# Events the CHILD will collect before it stops recording. The child stops
# tracing rather than growing a file: a 200MB trace nobody will read is a
# slower way to reach the same cap.
MAX_EVENTS = 4000

# Rendered characters. Sits inside MAX_TOOL_OUTPUT rather than on top of it --
# the same rule the MCP note follows, and for the same reason.
MAX_RENDER = 12_000

MAX_REPR = 120
DEFAULT_TIMEOUT = 180

# Written into the child. Kept as a string rather than importing this module
# there: the child is the user's interpreter running the user's program, and
# putting glmcode on its path could change which packages it resolves.
_TRACER = r'''
"""Injected by Make No Mistakes (glmcode/probe.py). Removed when the run ends."""
import json, os, sys, threading

# Chain to whatever sitecustomize the project already had, FIRST.
#
# Prepending our directory to PYTHONPATH is not enough and the test caught it:
# Python imports the FIRST sitecustomize it finds on sys.path and stops, so
# ours silently replaced theirs and their startup code never ran -- exactly the
# "do not change how the program under test starts" failure this module is
# supposed to avoid. Theirs runs before the tracer is installed, so their
# startup does not end up in the trace either.
try:
    _me = os.path.dirname(os.path.abspath(__file__))
    for _p in list(sys.path):
        try:
            if not _p or os.path.abspath(_p) == _me:
                continue
            _cand = os.path.join(_p, "sitecustomize.py")
            if os.path.isfile(_cand):
                with open(_cand, encoding="utf-8") as _f:
                    _src = _f.read()
                exec(compile(_src, _cand, "exec"),
                     {"__name__": "sitecustomize", "__file__": _cand})
                break
        except Exception:
            # Their sitecustomize raising is their problem and was already
            # happening before we arrived; it must not stop the program.
            pass
except Exception:
    pass

_OUT = os.environ.get("MNM_TRACE_OUT")
_ROOT = os.environ.get("MNM_TRACE_ROOT", "")
_MAX = int(os.environ.get("MNM_TRACE_MAX", "4000"))
_MAXREPR = int(os.environ.get("MNM_TRACE_REPR", "120"))
_FOCUS = os.environ.get("MNM_TRACE_FOCUS", "").strip()

if _OUT and _ROOT:
    _events = []
    _lock = threading.Lock()
    _stopped = [False]

    def _r(v):
        # Defensive on purpose: repr() raises on a half-built object, hits the
        # database on a lazy ORM row, and can be megabytes on an array. A
        # tracer that crashes or slows the program it is watching is worse
        # than no tracer.
        try:
            t = type(v).__name__
            if isinstance(v, (int, float, bool, type(None))):
                return repr(v)
            if isinstance(v, (str, bytes)):
                s = repr(v[:_MAXREPR])
                return s + ("..." if len(v) > _MAXREPR else "")
            if isinstance(v, BaseException):
                # str(), not the generic "<TypeError>" branch below. The
                # message IS the finding -- "unsupported operand type(s) for
                # *: 'NoneType' and 'int'" is the answer, and the class name
                # alone is what a traceback already said.
                m = str(v)[:_MAXREPR]
                return "%s(%s)" % (t, m) if m else t
            if isinstance(v, (list, tuple, set)):
                return "%s(len=%d)" % (t, len(v))
            if isinstance(v, dict):
                return "dict(keys=%s)" % (sorted(map(str, list(v)[:8])),)
            return "<%s>" % t
        except Exception:
            return "<unreprable>"

    def _mine(path):
        return bool(path) and path.startswith(_ROOT) and "site-packages" not in path

    def _tracer(frame, event, arg):
        if _stopped[0]:
            return None
        code = frame.f_code
        path = code.co_filename
        if not _mine(path):
            # Return None, not _tracer: this stops descending into a call we do
            # not care about, which is most of the speed of the whole thing.
            return None
        if _FOCUS and _FOCUS not in path and _FOCUS not in code.co_name:
            return _tracer
        if event == "call":
            try:
                names = code.co_varnames[:code.co_argcount]
                args = {n: _r(frame.f_locals.get(n)) for n in names}
            except Exception:
                args = {}
            _rec({"e": "call", "f": path, "l": frame.f_lineno,
                  "n": code.co_name, "a": args})
        elif event == "return":
            _rec({"e": "ret", "f": path, "n": code.co_name, "v": _r(arg)})
        elif event == "exception":
            try:
                exc_type, exc, _tb = arg
                if code.co_name == "<module>":
                    # A module frame's f_locals ARE its globals: every import,
                    # every dunder, and nothing about the failure. The frames
                    # below it carry the values that matter.
                    local = {}
                else:
                    local = {k: _r(v) for k, v in list(frame.f_locals.items())[:12]
                             if not k.startswith("__")}
            except Exception:
                exc_type, exc, local = None, None, {}
            _rec({"e": "exc", "f": path, "l": frame.f_lineno, "n": code.co_name,
                  "t": getattr(exc_type, "__name__", "?"), "m": _r(exc),
                  "locals": local})
        return _tracer

    def _rec(ev):
        with _lock:
            if len(_events) >= _MAX:
                _stopped[0] = True
                sys.settrace(None)
                threading.settrace(None)
                return
            _events.append(ev)

    def _dump():
        try:
            with open(_OUT, "w", encoding="utf-8") as f:
                json.dump({"events": _events, "capped": _stopped[0]}, f)
        except Exception:
            pass

    import atexit
    atexit.register(_dump)
    threading.settrace(_tracer)
    sys.settrace(_tracer)
'''


def _redact(text: str) -> str:
    """Run the project's own secret patterns over a trace before anyone sees it.

    Reusing scan_secrets' definitions rather than writing a second one: two
    ideas of what a secret looks like is one of them being wrong.
    """
    from .tools import _SECRET_PATTERNS
    out = text
    for label, rx in _SECRET_PATTERNS:
        try:
            out = rx.sub(f"<redacted {label}>", out)
        except Exception:
            continue
    return out


def _render(data: dict, root: Path) -> str:
    events = data.get("events") or []
    if not events:
        return ""
    lines, depth = [], 0
    for ev in events:
        kind = ev.get("e")
        try:
            where = str(Path(ev.get("f", "")).relative_to(root))
        except (ValueError, TypeError):
            where = ev.get("f", "?")
        if kind == "call":
            args = ", ".join(f"{k}={v}" for k, v in (ev.get("a") or {}).items())
            lines.append(f"{'  ' * depth}-> {ev.get('n')}({args})"
                         f"   [{where}:{ev.get('l')}]")
            depth = min(depth + 1, 12)
        elif kind == "ret":
            depth = max(depth - 1, 0)
            lines.append(f"{'  ' * depth}<- {ev.get('n')} returned {ev.get('v')}")
        elif kind == "exc":
            # The class name is recorded on its own, so drop the copy of it
            # the value repr carries: "TypeError: TypeError(...)" reads as a
            # bug in the tool.
            msg = str(ev.get("m") or "")
            klass = str(ev.get("t") or "")
            if klass and msg.startswith(klass + "(") and msg.endswith(")"):
                msg = msg[len(klass) + 1:-1]
            lines.append(f"{'  ' * depth}!! {klass}: {msg}"
                         f"   [{where}:{ev.get('l')}] in {ev.get('n')}")
            for k, v in (ev.get("locals") or {}).items():
                lines.append(f"{'  ' * (depth + 1)}{k} = {v}")
    text = "\n".join(lines)
    if data.get("capped"):
        text += (f"\n[trace stopped after {MAX_EVENTS} events — it was still "
                 f"running. Narrow it with `focus`.]")
    return text


def trace_run(command: str, focus: str = "", timeout_seconds: int = DEFAULT_TIMEOUT) -> str:
    """Run `command` with a tracer attached and report what actually happened.

    `focus` restricts recording to frames whose file path or function name
    contains it — the difference between a readable answer and four thousand
    events.
    """
    command = (command or "").strip()
    if not command:
        return "trace_run needs a command to run."
    root = Path(get_workdir()).resolve()
    timeout_seconds = max(1, min(int(timeout_seconds or DEFAULT_TIMEOUT), 600))

    with tempfile.TemporaryDirectory(prefix="mnm-trace-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "sitecustomize.py").write_text(_TRACER, encoding="utf-8")
        out_file = tmp_path / "trace.json"

        env = dict(os.environ)
        # Prepended, and the previous value preserved: a project that ships its
        # own sitecustomize would otherwise be silently replaced by this one,
        # which is a change to how the program under test starts up -- the one
        # thing a tracer must not do.
        env["PYTHONPATH"] = os.pathsep.join(
            [str(tmp_path)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        env["MNM_TRACE_OUT"] = str(out_file)
        env["MNM_TRACE_ROOT"] = str(root)
        env["MNM_TRACE_MAX"] = str(MAX_EVENTS)
        env["MNM_TRACE_REPR"] = str(MAX_REPR)
        env["MNM_TRACE_FOCUS"] = focus or ""

        from .tools import _shell_argv
        try:
            proc = subprocess.run(
                _shell_argv(command), cwd=str(root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=timeout_seconds, **NO_WINDOW_KWARGS)
            output = (proc.stdout or b"").decode("utf-8", errors="replace")
            code = proc.returncode
        except subprocess.TimeoutExpired:
            return (f"`{command}` did not finish within {timeout_seconds}s, so "
                    f"there is no trace. A tracer makes a program several times "
                    f"slower — raise timeout_seconds, or narrow it with `focus`.")
        except OSError as e:
            return f"Could not start `{command}`: {e}"

        try:
            data = json.loads(out_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            data = {}

    trace = _render(data, root) if data else ""
    header = f"$ {command}\nexit code: {code}"
    if not trace:
        # Said explicitly. A tracer that silently returns only stdout looks
        # like it worked and is the reason somebody would trust an empty
        # answer about a program that did plenty.
        return _truncate(
            f"{header}\n\n[no trace collected — nothing under {root} ran in a "
            f"Python interpreter. trace_run attaches through sitecustomize, so "
            f"it only sees Python; for anything else this is just the command's "
            f"output.]\n\n{output}")

    # The trace is the point, so it gets the budget; the command's own output
    # is trimmed to what is left. Both inside MAX_TOOL_OUTPUT, never on top.
    trace = _redact(_truncate(trace, MAX_RENDER))
    room = max(500, MAX_TOOL_OUTPUT - len(trace) - len(header) - 200)
    return (f"{header}\n\n--- what actually ran (project code only) ---\n{trace}"
            f"\n\n--- output ---\n{_truncate(output, room)}")
