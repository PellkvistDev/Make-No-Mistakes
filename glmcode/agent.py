"""The agentic loop: model <-> tools until the task is done.

Frontend-agnostic: all rendering and permission prompts go through an
AgentEvents sink (terminal: ui.ConsoleEvents, desktop app: gui.WebEvents).
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from pathlib import Path

from .api import ApiError, Cancelled, RateLimiter, Usage, ZaiClient, estimate_tokens
from .config import Config
from .events import AgentEvents
from .permissions import PermissionEngine
from .prompts import (ATTEMPT_TASK, BROWSER_AGENT_SYSTEM, BROWSER_RESUME_NOTE,
                      COMPACT_PROMPT, CONTINUE_NUDGE, CONVERSATIONAL_SYSTEM,
                      FRESH_CRITIC_SYSTEM, GREEN_GIVEUP_NUDGE, GREEN_NUDGE,
                      REFINE_NUDGE, STEER_NUDGE_TEMPLATE, STEP_LIMIT_NUDGE,
                      SUBAGENT_PREAMBLE, VIEW_IMAGE_PROMPT, VISION_ANALYSIS_PROMPT,
                      WRAP_UP_NUDGE, blind_critique_prompt, build_system_prompt,
                      conversational_project_context, detect_check_command,
                      fresh_review_nudge, is_critic_approval, verify_nudge)
from .tools import (BROWSER_ACTION_TOOLS, BROWSER_AGENT_SCHEMAS,
                    CHECK_WORKERS_TOOL, COMPACT_CONTEXT_TOOL,
                    CONTROL_CHROME_TOOL, CONVERSATIONAL_READONLY_SCHEMAS,
                    CONVERSATIONAL_SCHEMAS,
                    CHECK_PAGE_TOOL, DISPATCH_WORKER_TOOL, GENERATE_IMAGE_TOOL,
                    PREVIEW_PAGE_TOOL,
                    REMEMBER_TOOL, REVERT_WORKER_TOOL, REVIEW_CHANGES_TOOL,
                    SHOW_HTTP_CAT_TOOL, SHOW_IMAGE_TOOL, SPEAK_TOOL,
                    STEER_WORKER_TOOL, STOP_WORKER_TOOL, SUBAGENT_TOOL,
                    TOOL_SCHEMAS, VIEW_IMAGE_TOOL, WORKER_CHANGES_TOOL, ToolError,
                    clean_todo_items, execute_tool, set_call_token, set_workdir)

# Tools whose output tells the model whether its changes actually work --
# used by the verify-nudge (see _run_turn): a turn that edits files but never
# runs any of these gets one automatic push to verify before finishing.
VERIFICATION_TOOLS = {"run_powershell", "run_background", "run_tests",
                      "run_test_file", "preview_page"}
EDIT_TOOLS = {"write_file", "edit_file", "replace_in_files"}

MAX_SUBAGENTS = 6
# Safety cap on auto-continue-on-truncation rounds (see _call_model_until_done).
MAX_CONTINUATIONS = 3
# "Make it green": how many times the bounded test-fix loop will re-run the
# project's checks and feed a failure back before giving up and reporting.
GREEN_LOOP_MAX_ROUNDS = 4


def _first_line(text: str, limit: int = 280) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


def _spoken_permission(title: str, limit: int = 90) -> str:
    """A permission prompt title condensed into a phrase fit to speak aloud
    ('Run command: npm test' -> 'run command: npm test'), so the voice prompt
    sounds natural rather than reading a UI label."""
    t = (title or "do something").strip().splitlines()[0]
    if t and t[0].isupper() and not t[:3].isupper():  # keep acronyms as-is
        t = t[0].lower() + t[1:]
    return t[:limit]


def _msg_text(m: dict) -> str:
    """A message's text content, whether plain or multimodal parts."""
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
    return c or ""


def _final_report_text(messages: list) -> str:
    """The last assistant answer in a transcript, stitched back together if
    it was split across several messages by the auto-continuation-on-
    truncation logic (each split segment is followed by a CONTINUE_NUDGE
    user message, which is how we tell a split apart from a genuine earlier
    turn)."""
    parts: list[str] = []
    i = len(messages) - 1
    while i >= 0:
        m = messages[i]
        if m.get("role") != "assistant" or not isinstance(m.get("content"), str):
            break
        parts.append(m["content"])
        if (i >= 1 and messages[i - 1].get("role") == "user"
                and messages[i - 1].get("content") == CONTINUE_NUDGE):
            i -= 2
            continue
        break
    return "".join(reversed(parts)).strip()


class _CaptureEvents(AgentEvents):
    """Non-interactive event sink for a sub-agent: captures streamed text
    (for _run_single_subagent's final-report fallback chain) and forwards
    every granular event to the coordinator's own event sink, tagged with
    this sub-agent's id, so a UI can show its live thread -- not just the
    coarse start/done/error status from Agent.subagent(). By inheriting
    AgentEvents it also auto-denies any permission prompt (so a sub-agent
    can only do what the current mode allows without asking)."""

    def __init__(self, forward, aid: str, ask=None):
        self.text = ""
        self.last_error = ""  # last error()'d message, for the report fallback
        self._forward = forward
        self._aid = aid
        # Optional interactive permission handler: ask(title, preview,
        # always_label) -> 'y' | 'a' | ('n', feedback). Only set for workers
        # dispatched in conversational (voice) mode, so a worker's gated action
        # can be approved out loud instead of auto-denied. None keeps the
        # default sub-agent behavior (auto-deny).
        self._ask = ask

    def ask_permission(self, title: str, preview: str, always_label=None):
        if self._ask is not None:
            return self._ask(title, preview, always_label)
        return super().ask_permission(title, preview, always_label)

    def stream_start(self) -> None:
        self._forward(self._aid, "stream_start")

    def stream_end(self) -> None:
        self._forward(self._aid, "stream_end")

    def reasoning_delta(self, text: str) -> None:
        self._forward(self._aid, "reasoning", text=text)

    def content_delta(self, text: str) -> None:
        self.text += text
        self._forward(self._aid, "content", text=text)

    def tool_call(self, name: str, args: dict, call_id: str = "") -> None:
        self._forward(self._aid, "tool_call", name=name, args=args, call_id=call_id)

    def tool_result(self, name: str, content: str, is_error: bool = False) -> None:
        self._forward(self._aid, "tool_result", name=name, content=content, is_error=is_error)

    def steered(self, text: str) -> None:
        self._forward(self._aid, "steered", text=text)

    def steer_returned(self, text: str) -> None:
        self._forward(self._aid, "steer_returned", text=text)

    def wrapup_requested(self) -> None:
        self._forward(self._aid, "wrapup_requested")

    def browser_frame(self, url: str = "", image: str = "") -> None:
        self._forward(self._aid, "browser_frame", url=url, image=image)

    def info(self, msg: str) -> None:
        self._forward(self._aid, "notice", level="info", text=msg)

    def warn(self, msg: str) -> None:
        self._forward(self._aid, "notice", level="warn", text=msg)

    def error(self, msg: str) -> None:
        self.last_error = msg
        self._forward(self._aid, "notice", level="error", text=msg)


class Agent:
    def __init__(self, cfg: Config, client: ZaiClient, events: AgentEvents | None = None,
                 allow_subagents: bool = True, workdir: Path | None = None,
                 conversational: bool = False):
        self.cfg = cfg
        self.client = client
        # The project folder this agent works in. Pinned to its turn thread
        # via tools.set_workdir() at every run_turn, so parallel chats in
        # different folders can't contaminate each other through the
        # process-global cwd.
        self.workdir = Path(workdir) if workdir else Path.cwd()
        if events is None:
            from .ui import ConsoleEvents
            events = ConsoleEvents(cfg)
        self.events = events
        # path_rules is the SAME list object as cfg.path_rules (not a copy), so a
        # settings change that mutates it in place takes effect on every live
        # agent immediately (see gui set_setting). workdir anchors relative globs.
        self.permissions = PermissionEngine(
            mode=cfg.mode, path_rules=cfg.path_rules, workdir=self.workdir)
        self.messages: list[dict] = []
        self.session_usage = Usage()
        self.cancel = threading.Event()
        # Like cancel, but cooperative rather than immediate: doesn't abort
        # the current tool call, just skips straight to a forced final
        # answer (tools withheld) at the next safe checkpoint -- for a
        # sub-agent, "stop researching and write the report now" instead of
        # "stop instantly, no report at all" (see request_wrapup).
        self.wrap_up_requested = threading.Event()
        self.busy = False
        # Sub-agents don't get the spawning tool themselves (no recursion).
        self.allow_subagents = allow_subagents
        # Conversational (speech-to-speech) mode: the agent the user talks to by
        # voice is a pure delegator -- it does no file work itself, it only
        # converses and hands real work to fire-and-forget background workers
        # (dispatch_worker) so it never goes quiet mid-conversation. It gets a
        # tiny, dedicated tool set and a spoken-style system prompt.
        self.conversational = conversational
        if conversational:
            # Delegation tools + a read-only investigation set, so it can look
            # at the code to answer questions and decide what to delegate, but
            # never edit or run anything itself (that's the workers' job).
            self.tool_schemas = list(CONVERSATIONAL_SCHEMAS) + list(CONVERSATIONAL_READONLY_SCHEMAS)
        elif allow_subagents:
            self.tool_schemas = TOOL_SCHEMAS
        else:
            # Sub-agents don't get the spawning tool OR control_chrome (a browser
            # agent is spawned only by the coordinator, and no sub-agent recurses).
            self.tool_schemas = [
                s for s in TOOL_SCHEMAS
                if s["function"]["name"] not in (SUBAGENT_TOOL, CONTROL_CHROME_TOOL)
            ]
        # Fire-and-forget background workers dispatched in conversational mode.
        # Unlike spawn_agents (which joins), these are NOT waited on -- each runs
        # on its own daemon thread and reports back through worker_update events;
        # the registry survives across the conversational agent's short turns
        # (the Agent is persistent per chat).
        self._workers: dict[str, dict] = {}
        self._workers_lock = threading.Lock()
        self._worker_seq = 0
        # Approve-by-voice: a worker's gated action blocks on one of these until
        # the user answers out loud (or via the overlay buttons). rid -> {event,
        # answer}. Only used in conversational mode.
        self._worker_perms: dict[str, dict] = {}
        self._worker_perms_lock = threading.Lock()
        self._emit_lock = threading.Lock()  # serialize sub-agent progress emits
        # Steering: a message the user sends while this agent is mid-turn.
        # It doesn't interrupt the current model call -- it's queued (one at
        # a time) and injected as a plain user message the next time a tool
        # result comes back, right before the model is called again. If the
        # turn ends first (final answer / cancel / error) with nothing left
        # to attach it to, run_turn() hands it back via steer_returned()
        # instead of letting it leak into some future, unrelated turn.
        self._steer_pending: str | None = None
        self._steer_lock = threading.Lock()
        # Only meaningful on the coordinator instance (sub-agents don't spawn
        # their own), but harmless to keep everywhere: lets the GUI reach a
        # specific running sub-agent's Agent instance to steer it directly.
        self._active_subagents: dict[str, "Agent"] = {}
        self._active_subagents_lock = threading.Lock()
        # Optional append-only conversation log (see transcript.py). Set by
        # the frontend after construction; None (all hooks no-op) in the CLI
        # and for sub-agents (their reports land in the parent's transcript
        # via the spawn_agents tool result).
        self.transcript = None
        # Optional shadow-git BackupRepo (see backup.py), set by the GUI --
        # backs the review_changes tool ("what changed since this turn
        # started?"). Shared with sub-agents, who work in the same tree.
        self.backup_repo = None
        # Bring-your-own-model: a per-chat model id overriding cfg.model, and
        # an optional separate client for vision calls (set when the chat's
        # provider is a custom endpoint that can't serve the vision model --
        # vision keeps working through the built-in provider).
        self.model_override: str | None = None
        self.vision_client: ZaiClient | None = None
        # Vision "direct" mode: images the model asks to view (via view_image)
        # are embedded into the conversation instead of being described by the
        # GLM vision model, so a multimodal chat model sees them itself. Queued
        # here as (name, data_uri) and injected after the tool batch, mirroring
        # steering. Empty in "describe" mode (the default).
        self._pending_images: list[tuple[str, str]] = []
        self._routed_model_note: str | None = None  # de-dupe the routing notice
        # MCP: an optional McpManager whose external tools are appended to the
        # schema and dispatched via mcp.call. Shared process-wide (servers are
        # external processes); permission-gated like any non-readonly tool.
        self.mcp = None
        # Verify-nudge bookkeeping, reset each turn (see _run_turn).
        self._turn_wrote_files = False
        self._turn_verified = False
        self._verify_nudged = False
        # High/Max self-review bookkeeping, reset each turn (see _run_turn).
        self._refine_budget = 0
        self._refine_done = 0
        self._refine_pass_changed = False
        # Per-agent todo list (todo_write is handled in-dispatch): parallel
        # chats each keep their own checklist instead of sharing one global.
        self.todos: list[dict] = []
        # Interactive browser (control_chrome). Created lazily on first use and
        # kept alive at the chat level so cookies/login/current page persist
        # across control_chrome calls; a spawned Browser Agent shares this same
        # session to run its browser_* action tools. None until first used.
        self.browser_session = None
        self._browser_agent_aid: str | None = None  # the running Browser Agent, if any
        # Pause / take-over: only meaningful for the Browser Agent (pausable
        # set True there). The human can freeze the loop at a safe checkpoint,
        # drive the (headed, now-idle) browser themselves, then resume the SAME
        # agent -- which re-perceives the page and carries on with full memory.
        self.pausable = False
        self._pause_flag = threading.Event()
        self._resume_flag = threading.Event()
        self.rebuild_system_prompt()

    # ------------------------------------------------------------------ #

    def rebuild_system_prompt(self) -> None:
        # Cached separately from the live message so refreshing the context-
        # usage note (see _refresh_context_note) doesn't need to re-run
        # build_system_prompt's git subprocess calls on every model call.
        if self.conversational:
            self._base_system_prompt = (
                CONVERSATIONAL_SYSTEM
                + conversational_project_context(self.workdir)
                + self._conversational_language_note())
        else:
            self._base_system_prompt = build_system_prompt(self.workdir, self.cfg.model)
        if self.transcript:
            # Tell the model its transcript files exist and where, so it can
            # grep them for anything compacted out of context or said in a
            # past chat.
            self._base_system_prompt += self.transcript.prompt_note()
        sys_msg = {"role": "system", "content": self._with_usage_note(self._base_system_prompt)}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = sys_msg
        else:
            self.messages.insert(0, sys_msg)

    def _conversational_language_note(self) -> str:
        """Multilingual policy for voice mode: understand any spoken language,
        but keep the actual coding work in English (so speaking, say, Swedish
        doesn't degrade code quality), and reply in the user's chosen language."""
        reply = getattr(self.cfg, "voice_reply_language", "en")
        reply_line = ("Reply to the user out loud in the same language they spoke to you."
                      if reply == "match"
                      else "Reply to the user out loud in English.")
        return (
            "\n\n# Language\n"
            "The user may speak to you in any language — their speech is transcribed "
            "for you, so you might receive Swedish, English, or anything else. Whatever "
            "language they use:\n"
            "- ALWAYS reason in English, and ALWAYS write the tasks you hand to workers "
            "(dispatch_worker) in clear English. This keeps coding quality high and is "
            "required — never hand a worker a task written in another language. Do "
            "preserve any exact strings, identifiers, or literal text the user dictated "
            "(e.g. Swedish UI copy) verbatim inside the task.\n"
            f"- {reply_line}"
        )

    def _with_usage_note(self, base: str) -> str:
        est = estimate_tokens(self.messages) if self.messages else 0
        limit = self.cfg.context_limit_tokens
        return (
            f"{base}\n\n# Context usage\n"
            f"Estimated ~{est:,} of {limit:,} tokens used (rough estimate; auto-compact "
            f"triggers automatically above this if you don't act first). See the "
            f"compact_context tool in Tool usage policy."
        )

    def _refresh_context_note(self) -> None:
        """Cheap per-call refresh of just the usage note (no subprocess calls),
        so the model always sees an accurate, current figure."""
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._with_usage_note(self._base_system_prompt)

    def set_mode(self, mode: str) -> None:
        self.cfg.mode = mode
        self.permissions.mode = mode

    def clear(self) -> None:
        self.messages = []
        self.session_usage = Usage()
        self.rebuild_system_prompt()

    def load_messages(self, messages: list) -> None:
        """Restore a persisted conversation (system prompt rebuilt fresh for
        the current cwd/model rather than reusing whatever was saved)."""
        self.messages = [m for m in messages if m.get("role") != "system"]
        self.rebuild_system_prompt()

    def set_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.session_usage = Usage(prompt_tokens, completion_tokens)

    def request_cancel(self) -> None:
        self.cancel.set()

    # ------------------------------------------------------------------ #
    # Images

    def attach_images(self, text: str, image_paths: list[Path]) -> dict:
        """Build the user message for a turn that includes images.

        vision_route == "describe": ask the free vision model for an exhaustive
        analysis and inline it as text, keeping the strong coding model in charge.
        vision_route == "direct": embed images; the turn runs on the vision model.
        """
        names = ", ".join(p.name for p in image_paths)
        if self.cfg.vision_route == "direct":
            content: list = [
                {"type": "image_url", "image_url": {"url": self._encode(p)}}
                for p in image_paths
            ]
            content.append({"type": "text", "text": text or f"(user attached: {names})"})
            return {"role": "user", "content": content}

        with self.events.status(f"analyzing {names} with {self.cfg.vision_model}..."):
            analysis = self._client_for(self.cfg.vision_model).analyze_images(
                self.cfg.vision_model,
                VISION_ANALYSIS_PROMPT.format(user_text=text or "(no message)"),
                image_paths,
            )
        self.events.info(f"vision analysis of {names}: {len(analysis)} chars")
        combined = (
            f"{text}\n\n[Image analysis: {names} — produced by the vision model "
            f"from the image(s) the user attached]\n{analysis}"
        )
        return {"role": "user", "content": combined}

    @staticmethod
    def _encode(p: Path) -> str:
        from .api import encode_image_data_uri
        return encode_image_data_uri(p)

    def _copy_to_uploads(self, paths: list[Path]) -> list[str]:
        """Copy each file into the project's uploads/ folder; return a
        'name (see path)' reference for each (or a FAILED note)."""
        refs = []
        for p in paths:
            dest = self.workdir / "uploads" / f"{p.stem}-{uuid.uuid4().hex[:6]}{p.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(p, dest)
                refs.append(f"{p.name} (see {self._display_path(dest)})")
            except OSError as e:
                refs.append(f"{p.name} (FAILED to attach: {e})")
        return refs

    def attach_files(self, text: str, paths: list[Path]) -> dict:
        """Build the user message for a turn with attached files (any type,
        not just images). Unlike attach_images, nothing is read, encoded, or
        sent to any model here -- each file is copied into an uploads/
        folder in the project and the model gets a path reference, the same
        way it would find any other file already in the project. It decides
        for itself whether to read_file/view_image the attachment."""
        refs = self._copy_to_uploads(paths)
        note = ("The user attached a file: " if len(refs) == 1 else
                "The user attached files: ") + ", ".join(refs)
        combined = f"{text}\n\n[{note}]" if text else f"[{note}]"
        return {"role": "user", "content": combined}

    def attach_uploads(self, text: str, paths: list[Path],
                       embed_images: list[Path] | None = None) -> dict:
        """The desktop app's attachment handler, honoring vision_route:

        - "describe" (default): every uploaded file -> uploads/ + a path
          reference (the model reads/view_images them; images route through GLM
          vision). `embed_images` (from @-mentions) are left alone -- their
          clean paths are already in `text` for the model to view_image.
        - "direct": image files (uploaded AND @-mentioned) are embedded inline
          so a multimodal chat model sees them itself; non-image uploads still
          go to uploads/ + a reference.
        """
        paths = paths or []
        embed_images = embed_images or []
        if self.cfg.vision_route != "direct":
            return (self.attach_files(text, paths) if paths
                    else {"role": "user", "content": text})
        from .api import IMAGE_EXTENSIONS
        up_images = [p for p in paths if p.suffix.lower() in IMAGE_EXTENSIONS]
        others = [p for p in paths if p.suffix.lower() not in IMAGE_EXTENSIONS]
        parts: list = []
        for p in list(up_images) + list(embed_images):
            try:
                parts.append({"type": "image_url", "image_url": {"url": self._encode(p)}})
            except ValueError:
                others.append(p)  # too big to embed -> fall back to a file ref
        if not parts:
            return (self.attach_files(text, paths) if paths
                    else {"role": "user", "content": text})
        note = text or ""
        if others:
            refs = self._copy_to_uploads(others)
            note = (note + "\n\n" if note else "") + \
                "[The user also attached: " + ", ".join(refs) + "]"
        parts.append({"type": "text", "text": note or "(images attached)"})
        return {"role": "user", "content": parts}

    def _payload_has_images(self) -> bool:
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list) and any(
                part.get("type") == "image_url" for part in c
            ):
                return True
        return False

    def _model_for_turn(self) -> str:
        """Which model runs this step. Normally the chat's model, but images
        in the context force a routing decision:

        - "direct" mode with a custom (BYOM) model: keep the chat model -- the
          user set direct because their model is multimodal and should see the
          image itself.
        - otherwise (describe mode, or the built-in free model which can't see
          images on its coding model): route to the GLM vision model.
        """
        base = self.model_override or self.cfg.model
        if self._payload_has_images():
            if self.model_override and self.cfg.vision_route == "direct":
                target = base
            else:
                target = self.cfg.vision_model
        else:
            target = base
        if self._payload_has_images() and target != self._routed_model_note:
            self.events.info(f"images in context -> using {target}")
            self._routed_model_note = target
        return target

    def _inject_pending_images(self) -> None:
        """Flush images the model asked to view in direct mode into the
        conversation as a user message, so the next step's (multimodal) model
        sees them directly. Runs after the tool batch, like steering, so tool
        replies stay contiguous with their assistant message."""
        if not self._pending_images:
            return
        pending = self._pending_images
        self._pending_images = []
        content: list = [{"type": "image_url", "image_url": {"url": uri}}
                         for _, uri in pending]
        names = ", ".join(name for name, _ in pending)
        verb = "is" if len(pending) == 1 else "are"
        content.append({"type": "text",
                        "text": f"(Here {verb} the image{'' if len(pending) == 1 else 's'} "
                                f"you asked to view: {names})"})
        self.messages.append({"role": "user", "content": content})

    def _resolve_existing_image(self, path: str, tool_name: str) -> Path:
        """Resolve+validate a path argument that must point at an existing,
        supported image file. Shared by view_image and show_image. (Instance
        method: it resolves relative paths against this chat's workdir -- a
        stray @staticmethod here raised NameError on `self` for every relative
        path the model passed.)"""
        from .api import IMAGE_EXTENSIONS
        raw = str(path or "").strip()
        if not raw:
            raise ToolError(f"{tool_name} needs a 'path'")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.workdir / p
        if not p.is_file():
            raise ToolError(f"Image not found: {p}")
        if p.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ToolError(
                f"Not a supported image type ({p.suffix or '(none)'}): {p}. "
                f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
            )
        return p

    def _display_path(self, p: Path) -> str:
        """workdir-relative path for a nicer/portable tool-result marker, when
        possible. (Instance method: it needs this chat's workdir, and a stray
        @staticmethod here used to raise NameError on `self` for every upload.)"""
        try:
            return str(p.relative_to(self.workdir))
        except ValueError:
            return str(p)

    def _view_image(self, path: str, question: str = "") -> str:
        """The agent's own tool for looking at an image file (as opposed to
        attach_images, which handles an image the user attached)."""
        p = self._resolve_existing_image(path, "view_image")
        # Direct mode: embed the image into the conversation so a multimodal
        # chat model sees it itself, instead of getting a GLM-vision writeup.
        # If it's too big to embed, fall through to the describe path below.
        if self.cfg.vision_route == "direct":
            try:
                uri = self._encode(p)
            except ValueError:
                uri = None
            if uri is not None:
                self._pending_images.append((p.name, uri))
                return (f"[Attached {p.name} to the conversation -- you'll see the "
                        f"image itself on your next step; look at it and continue.]")
        focus = (f"What the agent needs to know: {question.strip()}" if question and question.strip()
                 else "No specific focus was given; describe the image exhaustively.")
        prompt = VIEW_IMAGE_PROMPT.format(focus=focus)
        with self.events.status(f"looking at {p.name} with {self.cfg.vision_model}..."):
            try:
                result = self._client_for(self.cfg.vision_model).analyze_images(
                    self.cfg.vision_model, prompt, [p])
            except ValueError as e:  # e.g. encode_image_data_uri's size-limit check
                raise ToolError(str(e))
        return result.strip() or "(vision model returned no description)"

    def _asset_marker(self, kind: str, p: Path, caption: str, note: str) -> str:
        """Tool-result text carrying a machine-parseable marker so sessions.py
        can reconstruct an inline image/audio card when a session is reopened
        later (see sessions.to_display / _extract_asset_marker)."""
        marker = f"[{kind}: {self._display_path(p)}]"
        if caption:
            marker += f" [caption: {caption}]"
        return f"{marker} {note}"

    def _show_image_tool(self, path: str, caption: str = "") -> str:
        """Show an existing image file to the user inline in the chat. Purely
        a UI side-channel -- unlike view_image, nothing is sent to the vision
        model or added to the text model's context."""
        p = self._resolve_existing_image(path, "show_image")
        self.events.show_image(str(p), caption=caption or "")
        return self._asset_marker("image", p, caption, "Displayed to the user.")

    def _show_http_cat_tool(self, status_code: int) -> str:
        """Fetch and show the http.cat image for an HTTP status code."""
        from .tools import fetch_http_cat
        out_path = self.workdir / "generated" / f"http-cat-{int(status_code)}-{uuid.uuid4().hex[:6]}"
        try:
            saved = fetch_http_cat(int(status_code), out_path)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"could not fetch http.cat image: {e}")
        caption = f"HTTP {status_code}"
        self.events.show_image(str(saved), caption=caption)
        return self._asset_marker("image", saved, caption, "Displayed to the user.")

    def _preview_page_tool(self, url: str, wait_seconds: float = 2.0) -> str:
        """Screenshot a URL (e.g. a local dev server) with headless Chromium."""
        from .browser import preview_page
        url = (url or "").strip()
        if not url:
            raise ToolError("preview_page needs a 'url'")
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:40].strip("-") or "page"
        out_path = self.workdir / "generated" / f"preview-{slug}-{uuid.uuid4().hex[:6]}.png"

        with self.events.status(f"loading {url} in a headless browser..."):
            try:
                saved = preview_page(url, out_path, wait_seconds=wait_seconds, status=self.events.info)
            except Exception as e:
                raise ToolError(f"page preview failed: {e}")

        self.events.show_image(str(saved), caption=url)
        return self._asset_marker(
            "image", saved, url,
            "Screenshot captured and shown to the user. Call view_image on this path if "
            "you need a detailed description of what rendered."
        )

    def _check_page_tool(self, url: str, wait_seconds: float = 2.5) -> str:
        """Load a running page and report runtime console/JS errors + failed
        requests alongside a screenshot, so the agent can fix what breaks when
        the app actually runs."""
        from .browser import capture_page
        url = (url or "").strip()
        if not url:
            raise ToolError("check_page needs a 'url'")
        slug = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")[:40].strip("-") or "page"
        out_path = self.workdir / "generated" / f"runtime-{slug}-{uuid.uuid4().hex[:6]}.png"
        with self.events.status(f"running {url} and watching for errors..."):
            try:
                r = capture_page(url, out_path, wait_seconds=wait_seconds, status=self.events.info)
            except Exception as e:
                raise ToolError(f"could not run the page: {e}")

        if r.get("screenshot"):
            self.events.show_image(r["screenshot"], caption=url)
        lines = []
        if r.get("load_error"):
            lines.append(f"LOAD ERROR: {r['load_error']}")
        for label, key in (("Uncaught JS errors", "page_errors"),
                           ("Console errors/warnings", "console"),
                           ("Failed network requests", "failed_requests")):
            items = r.get(key) or []
            if items:
                lines.append(f"{label} ({len(items)}):")
                lines.extend(f"  - {it}" for it in items)
        clean = not lines
        marker = self._asset_marker("image", Path(r["screenshot"]), url,
                                    "Screenshot shown to the user.") if r.get("screenshot") else ""
        if clean:
            return f"{url} loaded with no console errors, JS exceptions, or failed requests. {marker}"
        return (f"Runtime check of {url} found problems — fix them and check again:\n"
                + "\n".join(lines) + f"\n\n{marker}")

    def _generate_image(self, prompt: str, path: str = "", steps: int = 1) -> str:
        """Generate an image locally with sd-turbo and show it to the user."""
        from .imagegen import generate_image
        prompt = (prompt or "").strip()
        if not prompt:
            raise ToolError("generate_image needs a 'prompt'")

        if path and path.strip():
            out_path = Path(path.strip()).expanduser()
            if not out_path.is_absolute():
                out_path = self.workdir / out_path
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")[:40].strip("-") or "image"
            out_path = self.workdir / "generated" / f"{slug}-{uuid.uuid4().hex[:6]}.png"

        with self.events.status(f"generating image: {prompt[:60]}..."):
            try:
                saved = generate_image(prompt, out_path, steps=steps, status=self.events.info)
            except Exception as e:
                raise ToolError(f"image generation failed: {e}")

        self.events.show_image(str(saved), caption=prompt)
        return self._asset_marker("image", saved, prompt, "Generated and shown to the user.")

    def _speak_tool(self, text: str, path: str = "", voice: str = "", speed: float | None = None) -> str:
        """Generate speech locally and play it for the user, using the
        configured TTS engine (Kokoro or Piper). Defaults to the user's
        Settings voice/speed (the same ones the read-aloud toggle uses) unless
        the model explicitly asks for something different."""
        from . import tts_engine
        engine = (getattr(self.cfg, "tts_engine", "kokoro") or "kokoro")
        default_voice = tts_engine.default_voice(engine)
        cfg_voice = (self.cfg.piper_voice if engine == "piper" else self.cfg.tts_voice)
        text = (text or "").strip()
        if not text:
            raise ToolError("speak needs a 'text'")
        requested_voice = (voice or "").strip()
        if requested_voice:
            # An invalid id would otherwise be silently swapped for the default
            # inside synthesize() -- correct but confusing, since the model never
            # finds out its request didn't apply. Only validate an EXPLICIT
            # request; the user's own configured voice is trusted as-is.
            valid = tts_engine.list_voices(engine)
            if requested_voice not in valid:
                raise ToolError(
                    f"'{requested_voice}' is not a valid {engine} voice id. Valid ids: "
                    f"{', '.join(valid)}"
                )
        voice = requested_voice or cfg_voice or default_voice
        speed = speed if speed is not None else (self.cfg.tts_speed or 1.0)

        if path and path.strip():
            out_path = Path(path.strip()).expanduser()
            if not out_path.is_absolute():
                out_path = self.workdir / out_path
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "speech"
            out_path = self.workdir / "generated" / f"{slug}-{uuid.uuid4().hex[:6]}.wav"

        with self.events.status(f"generating speech: {text[:60]}..."):
            try:
                saved = tts_engine.save_wav(text, out_path, voice=voice, speed=speed,
                                            engine=engine, status=self.events.info)
            except Exception as e:
                raise ToolError(f"speech generation failed: {e}")

        self.events.show_audio(str(saved), caption=text)
        return self._asset_marker("audio", saved, text, "Generated and played for the user.")

    def _review_changes_tool(self) -> str:
        """Diff of the work-tree against the automatic pre-turn snapshot."""
        if self.backup_repo is None:
            raise ToolError(
                "change tracking isn't available here (auto-backup is off for "
                "this chat, or this is the terminal version) -- use git_diff "
                "or read the files directly instead")
        return self.backup_repo.turn_diff()

    # ------------------------------------------------------------------ #
    # Main loop

    def run_turn(self, user_message: dict) -> None:
        """One user turn: append the message, loop model+tools until done."""
        set_workdir(self.workdir)  # pin tool path resolution to THIS thread
        self.cancel.clear()
        self.wrap_up_requested.clear()
        self.busy = True
        try:
            if self._should_race():
                self._run_parallel_attempts(user_message)
            else:
                self._run_turn(user_message)
        finally:
            self.busy = False
            # If a steering message was queued but the turn ended (final
            # answer, cancel, or error) before it ever got a chance to be
            # injected -- there was no further tool result to attach it to --
            # it must NOT sit around and get silently glued onto some later,
            # unrelated turn. Hand it back so the frontend can put it back
            # wherever the user typed it.
            leftover = self._take_pending_steer()
            if leftover:
                self.events.steer_returned(leftover)
            self.events.turn_done(self.session_usage, self.context_estimate())

    def steer(self, text: str) -> bool:
        """Queue a message from the user while this agent is mid-turn. It
        doesn't interrupt whatever's in flight -- it's picked up and injected
        as a plain user message the next time a tool result comes back. Only
        one message may be queued at a time; returns False if one already is
        (the caller should edit/clear it first)."""
        text = (text or "").strip()
        if not text:
            return False
        with self._steer_lock:
            if self._steer_pending is not None:
                return False
            self._steer_pending = text
        return True

    def clear_steer(self) -> None:
        """Drop the queued steering message, if any, without delivering it."""
        with self._steer_lock:
            self._steer_pending = None

    def steer_subagent(self, aid: str, text: str) -> bool:
        """Forward a steering message to a specific running sub-agent by id.
        Returns False if that sub-agent isn't currently running, or already
        has a message queued."""
        with self._active_subagents_lock:
            sub = self._active_subagents.get(aid)
        if sub is None:
            return False
        return sub.steer(text)

    def request_wrapup(self) -> None:
        """Cooperatively force this agent to stop calling tools and write its
        final report at the next safe checkpoint (after whatever tool call is
        currently in flight finishes), instead of continuing to work."""
        self.wrap_up_requested.set()

    def wrapup_subagent(self, aid: str) -> bool:
        """Forward a wrap-up request to a specific running sub-agent by id.
        Returns False if that sub-agent isn't currently running."""
        with self._active_subagents_lock:
            sub = self._active_subagents.get(aid)
        if sub is None:
            return False
        sub.request_wrapup()
        return True

    def clear_steer_subagent(self, aid: str) -> bool:
        with self._active_subagents_lock:
            sub = self._active_subagents.get(aid)
        if sub is None:
            return False
        sub.clear_steer()
        return True

    # -- pause / take-over (Browser Agent) -------------------------------- #

    def request_pause(self) -> bool:
        """Ask this agent to freeze at the next safe checkpoint (after the
        in-flight tool finishes). Only pausable agents honor it."""
        if not self.pausable:
            return False
        self._resume_flag.clear()
        self._pause_flag.set()
        return True

    def request_resume(self) -> bool:
        if not self.pausable:
            return False
        self._resume_flag.set()
        return True

    @property
    def is_paused(self) -> bool:
        return self._pause_flag.is_set()

    def _paused_browser_agents(self) -> list:
        with self._active_subagents_lock:
            return [s for s in self._active_subagents.values()
                    if getattr(s, "pausable", False)]

    def pause_browser_agent(self) -> bool:
        """Coordinator entry point: freeze the running Browser Agent so the
        user can take over its browser window. Returns False if none is
        running."""
        subs = self._paused_browser_agents()
        for s in subs:
            s.request_pause()
        if subs and self._browser_agent_aid:
            self._emit_subagent(self._browser_agent_aid, "browser", "paused",
                                summary="paused — you have the browser")
        return bool(subs)

    def resume_browser_agent(self) -> bool:
        """Resume the Browser Agent; it re-reads the (possibly user-changed)
        page and continues its mission."""
        subs = self._paused_browser_agents()
        for s in subs:
            s.request_resume()
        if subs and self._browser_agent_aid:
            self._emit_subagent(self._browser_agent_aid, "browser", "running",
                                summary="resumed")
        return bool(subs)

    def _maybe_pause(self) -> None:
        """Loop checkpoint: if paused, block here until resumed (or cancelled/
        wrapped up). The browser sits idle and open while blocked, so the human
        can drive it; on resume, tell the agent the page may have changed."""
        if not (self.pausable and self._pause_flag.is_set()):
            return
        resumed = False
        while self._pause_flag.is_set():
            if self.cancel.is_set() or self.wrap_up_requested.is_set():
                break
            if self._resume_flag.wait(timeout=0.3):
                resumed = True
                break
        self._resume_flag.clear()
        self._pause_flag.clear()
        if resumed:
            self.messages.append({"role": "user", "content": BROWSER_RESUME_NOTE})

    def _take_pending_steer(self) -> str | None:
        with self._steer_lock:
            text, self._steer_pending = self._steer_pending, None
        return text

    def _inject_steer_messages(self) -> None:
        """Drain the queued steering message, if any, into the conversation,
        and let the event sink show it arrived. Wrapped with STEER_NUDGE_TEMPLATE
        rather than sent as a bare user message: an unframed message mid-turn
        reads to the model as a brand-new top-level instruction with equal
        weight to the original task, which is why steering used to blow past
        its scope entirely instead of just tweaking what was already underway."""
        text = self._take_pending_steer()
        if not text:
            return
        self.messages.append({"role": "user", "content": STEER_NUDGE_TEMPLATE.format(text=text)})
        if self.transcript:
            self.transcript.user(text, label="User (steering)")
        self.events.steered(text)

    def _log_assistant_msg(self, msg: dict) -> None:
        """Append an assistant message (text and/or tool calls) to the
        transcript, if one is attached. Call right after every
        self.messages.append(<assistant message>) site."""
        if not self.transcript:
            return
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            calls.append(f"{fn.get('name', '?')}({(fn.get('arguments') or '')[:300]})")
        self.transcript.assistant(msg.get("content") or "", calls)

    def _run_turn(self, user_message: dict) -> None:
        self.maybe_autocompact()
        self.messages.append(user_message)
        if self.transcript:
            self.transcript.user(_msg_text(user_message))
        self._turn_wrote_files = False
        self._turn_verified = False
        self._verify_nudged = False
        # The user's request this turn, kept for the fresh-eyes reviewer (which
        # judges the diff against the task in a clean, reasoning-free context).
        self._turn_task = _msg_text(user_message)
        # "Make it green" bookkeeping, reset each turn (see _green_step).
        self._green_rounds = 0
        self._green_done = False
        # High/Max thinking modes: after the main answer, run self-review passes
        # that re-examine the work and fix issues. Only the main chat agent
        # refines -- sub-agents, workers and the voice delegator do not (it would
        # multiply latency and isn't where the quality payoff is).
        from .config import THINKING_REFINE_PASSES
        self._refine_budget = (THINKING_REFINE_PASSES.get(self.cfg.thinking_mode, 0)
                               if (self.allow_subagents and not self.conversational) else 0)
        self._refine_done = 0
        self._refine_pass_changed = False

        for iteration in range(self.cfg.max_turns_per_request):
            # Recomputed each step: images can enter the context mid-turn (the
            # model calls view_image), which changes where the turn must run.
            model = self._model_for_turn()
            try:
                result = self._call_model_until_done(model)
            except ApiError as e:
                self.events.error(str(e))
                if e.status in (401, 403):
                    self.events.warn(
                        "Check the ZAI_API_KEY environment variable "
                        "(setx ZAI_API_KEY your-key). Keys are free at https://z.ai")
                return
            except (Cancelled, KeyboardInterrupt):
                self.events.warn("interrupted")
                self.messages.append({
                    "role": "assistant",
                    "content": "(response interrupted by user)",
                })
                if self.transcript:
                    self.transcript.marker("response interrupted by user")
                return

            self.session_usage.add(result.usage)
            msg = result.to_message()
            self.messages.append(msg)
            self._log_assistant_msg(msg)

            if not result.tool_calls:
                # "Make it green" (opt-in): after an edit turn, actually RUN the
                # project's checks and, if they fail, feed the failure back and
                # keep fixing -- bounded, and only when there's something to run.
                # Deliberately gated so it never fires on a small/no-test/passing
                # task: enabled + this is the main agent + files were edited.
                if (self.cfg.auto_fix_tests and self.allow_subagents
                        and not self.conversational and self._turn_wrote_files
                        and not self._green_done):
                    if self._green_step():
                        continue
                # One automatic push before accepting a final answer: a turn
                # that edited files but never ran ANYTHING has skipped the
                # "Verify" step entirely -- ask once, then accept whatever
                # the model decides (it may legitimately decline).
                if (self.cfg.verify_edits and self._turn_wrote_files
                        and not self._turn_verified and not self._verify_nudged):
                    self._verify_nudged = True
                    self.events.info("files were edited but nothing was run -- "
                                     "asking the agent to verify its changes")
                    self.messages.append({"role": "user",
                                          "content": verify_nudge(self.workdir)})
                    continue
                # High/Max: run a review pass over the answer. The first pass
                # always runs; further passes (Max) only run if the previous one
                # actually changed something -- once a review finds nothing to
                # fix, more reviews are just wasted work.
                if self._refine_done < self._refine_budget and \
                        (self._refine_done == 0 or self._refine_pass_changed):
                    nudge = self._refine_nudge()
                    if nudge is None:
                        return  # independent review approved the work as-is
                    self._refine_done += 1
                    self._refine_pass_changed = False
                    self.events.info(f"reviewing and improving the answer "
                                     f"(pass {self._refine_done})…")
                    self.messages.append({"role": "user", "content": nudge})
                    continue
                return  # final answer already streamed

            try:
                self._handle_tool_calls(result.tool_calls)
            except (Cancelled, KeyboardInterrupt):
                self.events.warn("interrupted during tool execution")
                return

            self._inject_steer_messages()
            self._inject_pending_images()
            self._maybe_pause()

            if self.wrap_up_requested.is_set():
                self.wrap_up_requested.clear()
                self.events.wrapup_requested()
                self._forced_wrapup(model, WRAP_UP_NUDGE)
                return

        # The loop ran out of steps without the model ever giving a plain-
        # text answer -- its last action was a tool call, so self.messages
        # currently ends on an assistant turn with no reportable content.
        # For the main agent that's recoverable (the user can just say
        # "continue"), but a sub-agent gets exactly one turn and its ENTIRE
        # value is this final report (see _run_single_subagent) -- silently
        # stopping here is why sub-agents were so often coming back with
        # "(sub-agent produced no final report)". Force one last call with
        # tools withheld so it can't just make another one instead of
        # answering.
        self._forced_wrapup(model, STEP_LIMIT_NUDGE)
        self.events.warn(f"stopped after {self.cfg.max_turns_per_request} agentic "
                         "steps; say 'continue' to let it keep going")

    def _forced_wrapup(self, model: str, nudge: str) -> None:
        """Append `nudge` and force one last call with tools withheld, so the
        model can't just make another tool call instead of answering. Used
        both when the step-limit is hit and when the user explicitly asks a
        sub-agent to wrap up early (see request_wrapup/wrapup_subagent)."""
        self.messages.append({"role": "user", "content": nudge})
        # Framed with stream_start/stream_end like every other model call --
        # without them the GUI never reset its per-round render state (the
        # wrap-up text also skipped the buffered-flush path), and the
        # terminal UI's live spinner state got confused.
        self.events.stream_start()
        try:
            result = self._client_for(model).chat(
                model=model, messages=self.messages, tools=None,
                temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                thinking=False, on_content=self.events.content_delta,
                on_reasoning=self.events.reasoning_delta, on_status=self.events.info,
                cancel=self.cancel,
            )
            self.session_usage.add(result.usage)
            msg = result.to_message()
            self.messages.append(msg)
            self._log_assistant_msg(msg)
        except (ApiError, Cancelled, KeyboardInterrupt):
            pass  # best-effort wrap-up either way
        finally:
            self.events.stream_end()

    # -- "make it green": bounded test-fix loop --------------------------- #

    def _run_check(self, cmd: str) -> tuple[bool, str]:
        """Run the project's check command in this chat's workdir and return
        (passed, output). Isolated for testing (monkeypatched in unit tests)."""
        from .tools import run_check_command
        code, output = run_check_command(cmd)
        return code == 0, output

    def _green_step(self) -> bool:
        """One iteration of the make-it-green loop. Runs the detected check
        command; returns True if it injected a fix nudge and the turn loop should
        continue, False when there's nothing to run or the tests already pass.
        A real run here also counts as verification (so the verify-nudge, if
        enabled, won't also fire)."""
        cmd = detect_check_command(self.workdir)
        if not cmd:
            self._green_done = True   # nothing detectable to run -- don't retry
            return False
        self.events.info(f"running the project's tests ({cmd})…")
        try:
            passed, output = self._run_check(cmd)
        except Exception as e:
            self._green_done = True
            self.events.warn(f"couldn't run the tests: {e}")
            return False
        self._turn_verified = True
        if passed:
            self._green_done = True
            self.events.info("tests pass.")
            return False
        if self._green_rounds >= GREEN_LOOP_MAX_ROUNDS:
            self._green_done = True
            self.events.warn(f"tests still failing after {GREEN_LOOP_MAX_ROUNDS} "
                             "fix attempts — stopping and reporting.")
            self.messages.append({"role": "user",
                                  "content": GREEN_GIVEUP_NUDGE.format(cmd=cmd, output=output)})
            return True
        self._green_rounds += 1
        self.events.warn(f"tests failing — fixing (attempt {self._green_rounds}/"
                         f"{GREEN_LOOP_MAX_ROUNDS})…")
        self.messages.append({"role": "user",
                              "content": GREEN_NUDGE.format(cmd=cmd, output=output)})
        return True

    # -- parallel attempts ("race", best-of-N) ---------------------------- #

    def _should_race(self) -> bool:
        """Race mode needs >1 attempts AND change tracking (the shadow-git repo)
        to snapshot a common baseline, isolate each attempt by reverting to it,
        and restore the winner. Only the main agent races (never a sub-agent /
        attempt / worker / voice)."""
        from .backup import available as backup_available
        return (getattr(self.cfg, "parallel_attempts", 1) > 1
                and self.allow_subagents and not self.conversational
                and self.backup_repo is not None and backup_available())

    def _make_attempt_agent(self, aid: str) -> "Agent":
        sub = Agent(self.cfg, self.client,
                    events=_CaptureEvents(self._emit_subagent_stream, aid),
                    allow_subagents=False, workdir=self.workdir)
        sub.model_override = self.model_override
        sub.vision_client = self.vision_client
        sub.backup_repo = self.backup_repo
        sub.mcp = self.mcp
        sub.permissions.mode = self.permissions.mode
        return sub

    def _score_attempt(self) -> tuple[bool | None, str]:
        """Run the project's checks on the current work-tree. Returns
        (passed, output): True/False, or None when there's nothing to run."""
        cmd = detect_check_command(self.workdir)
        if not cmd:
            return None, ""
        try:
            return self._run_check(cmd)
        except Exception:
            return None, ""

    @staticmethod
    def _pick_winner(results: list[dict]) -> dict:
        """Best attempt: tests passing beats unknown beats failing; a real change
        beats none; earlier attempt wins ties (so we don't churn for no reason)."""
        def rank(r):
            tier = 2 if r["passed"] is True else (1 if r["passed"] is None else 0)
            return (tier, 1 if r["changed"] else 0, -r["attempt"])
        return max(results, key=rank)

    def _run_parallel_attempts(self, user_message: dict) -> None:
        self.maybe_autocompact()
        self.messages.append(user_message)
        if self.transcript:
            self.transcript.user(_msg_text(user_message))
        n = min(max(int(self.cfg.parallel_attempts), 2), 3)
        baseline = self.backup_repo.snapshot("race baseline")
        if not baseline:  # git hiccup -> just run a single normal turn
            self.messages.pop()
            self._run_turn(user_message)
            return
        task = _msg_text(user_message)
        self.events.info(f"racing {n} independent attempts…")
        results: list[dict] = []
        for k in range(1, n + 1):
            if self.cancel.is_set():
                break
            if k > 1:
                self.backup_repo.revert_to(baseline)  # each attempt starts clean
            aid = f"attempt-{k}"
            self._emit_subagent(aid, f"attempt {k}", "running",
                                mission=f"attempt {k} of {n}")
            sub = self._make_attempt_agent(aid)
            try:
                sub.run_turn({"role": "user",
                              "content": ATTEMPT_TASK.format(task=task, k=k, n=n)})
            except (Cancelled, KeyboardInterrupt):
                self._emit_subagent(aid, f"attempt {k}", "error", summary="cancelled")
                break
            commit = self.backup_repo.snapshot(f"attempt {k}")
            passed, output = self._score_attempt()
            changed = bool(self.backup_repo.changed_files_since(baseline))
            report = _final_report_text(sub.messages)
            verdict = ("tests pass" if passed else "tests fail" if passed is False
                       else "no tests")
            self._emit_subagent(aid, f"attempt {k}", "done",
                                summary=f"{verdict}; {'changes made' if changed else 'no changes'}")
            results.append({"attempt": k, "commit": commit, "passed": passed,
                            "output": output, "changed": changed, "report": report})

        if not results:  # cancelled before any attempt finished
            self.backup_repo.revert_to(baseline)
            self.messages.append({"role": "assistant",
                                  "content": "(attempts cancelled before any finished)"})
            return
        winner = self._pick_winner(results)
        if winner["commit"]:
            self.backup_repo.revert_to(winner["commit"])  # keep the winner's files
        summary = self._race_summary(results, winner)
        self.events.stream_start()
        self.events.content_delta(summary)
        self.events.stream_end()
        msg = {"role": "assistant", "content": summary}
        self.messages.append(msg)
        self._log_assistant_msg(msg)

    def _race_summary(self, results: list[dict], winner: dict) -> str:
        n = len(results)
        lines = [f"Ran {n} independent attempt{'s' if n != 1 else ''} and kept "
                 f"attempt {winner['attempt']}.\n"]
        for r in results:
            mark = "✓ kept" if r is winner else "·"
            verdict = ("tests pass" if r["passed"] else "tests fail"
                       if r["passed"] is False else "no tests to run")
            lines.append(f"- Attempt {r['attempt']}: {verdict}"
                         f"{'' if r['changed'] else ', no changes'} {mark}")
        lines.append("")
        lines.append(winner["report"] or "(the winning attempt left no written summary)")
        return "\n".join(lines)

    # -- fresh-eyes review (High/Max) ------------------------------------- #

    def _refine_nudge(self) -> str | None:
        """The message that drives one High/Max review pass, or None to stop.

        When there's a real diff to judge, run an INDEPENDENT critic in a clean
        context (task + diff only, no reasoning) and feed its concrete findings
        back for the main agent to fix -- returning None if it approved the work.
        With no change tracking, or no diff, or the critic unavailable, fall back
        to the in-context self-review (REFINE_NUDGE), which also handles prose-
        only answers that have nothing to diff."""
        diff = ""
        if self.backup_repo is not None:
            try:
                diff = self.backup_repo.turn_diff()
            except Exception:
                diff = ""
        has_diff = bool(diff) and not diff.startswith(
            ("No changes", "(no pre-turn", "(git is not", "(could not"))
        if not has_diff:
            return REFINE_NUDGE
        critique = self._blind_critique(self._turn_task, diff)
        if not critique:
            return REFINE_NUDGE  # reviewer unavailable -> self-review fallback
        if is_critic_approval(critique):
            self.events.info("independent review found nothing to fix")
            return None
        return fresh_review_nudge(critique)

    def _blind_critique(self, task: str, diff: str) -> str:
        """One independent-reviewer model call over a fresh, minimal context so
        it can't just rubber-stamp its own reasoning. Returns the critique text
        ('APPROVED' when it's happy), or "" if the call failed (caller falls
        back). Not streamed to the UI -- it's internal plumbing."""
        model = self.model_override or self.cfg.model
        msgs = [
            {"role": "system", "content": FRESH_CRITIC_SYSTEM},
            {"role": "user", "content": blind_critique_prompt(task, diff)},
        ]
        try:
            result = self._client_for(model).chat(
                model=model, messages=msgs, tools=None,
                temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens,
                thinking=(self.cfg.thinking_mode != "low") and model != self.model_override,
                cancel=self.cancel,
            )
        except (ApiError, Cancelled, KeyboardInterrupt):
            return ""
        except Exception:
            return ""
        return (result.content or "").strip()

    def _tools_for_call(self) -> list:
        """Built-in tool schemas plus whatever MCP servers currently expose
        (recomputed per call: servers can come up/go down mid-chat)."""
        if self.mcp is None:
            return self.tool_schemas
        try:
            extra = self.mcp.tool_schemas()
        except Exception:
            extra = []
        return self.tool_schemas + extra if extra else self.tool_schemas

    def _client_for(self, model: str) -> ZaiClient:
        """The client to use for a given model id: vision calls go through
        the dedicated vision client when one is set (custom providers can't
        serve the built-in vision model); everything else uses the chat's
        own client."""
        if self.vision_client is not None and model == self.cfg.vision_model:
            return self.vision_client
        return self.client

    def _call_model(self, model: str):
        self._refresh_context_note()
        self.events.stream_start()
        try:
            return self._client_for(model).chat(
                model=model,
                messages=self.messages,
                tools=self._tools_for_call(),
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
                # `thinking` becomes GLM's z.ai-specific {"type":"enabled"}
                # payload field -- only send it to the built-in provider, never
                # to a custom BYOM endpoint (Ollama/OpenRouter/etc.) which would
                # reject or choke on it. model != model_override is True for the
                # built-in chat model AND for the GLM vision model routed through
                # the built-in client; False only for the custom model itself.
                thinking=(self.cfg.thinking_mode != "low") and model != self.model_override,
                on_content=self.events.content_delta,
                on_reasoning=self.events.reasoning_delta,
                on_status=self.events.info,
                cancel=self.cancel,
            )
        finally:
            self.events.stream_end()

    def _call_model_until_done(self, model: str):
        """Like _call_model, but if the response gets cut off by the output
        token limit before the model finishes (no tool calls emitted either),
        automatically nudge it to continue instead of silently treating the
        truncated fragment as a finished answer. This is what was causing
        replies -- and sub-agent reports especially, since a verbose report
        can easily blow past max_tokens -- to just stop with little or no
        text."""
        result = self._call_model(model)
        nudges = 0
        while (result.finish_reason == "length" and not result.tool_calls
               and nudges < MAX_CONTINUATIONS):
            nudges += 1
            self.events.warn(
                f"response hit the output limit; continuing automatically "
                f"({nudges}/{MAX_CONTINUATIONS})..."
            )
            frag = result.to_message()
            self.messages.append(frag)
            self._log_assistant_msg(frag)  # each split fragment, in order
            self.messages.append({"role": "user", "content": CONTINUE_NUDGE})
            result = self._call_model(model)
        if result.finish_reason == "length" and not result.tool_calls:
            self.events.warn(
                "response still hit the output limit after automatic "
                "continuation; it may be incomplete."
            )
        return result

    # ------------------------------------------------------------------ #

    def _handle_tool_calls(self, tool_calls: list) -> None:
        # Index of the assistant turn these tool_calls belong to. Captured
        # once, up front: it's unambiguously self.messages[-1] right now
        # (nothing else has been appended yet), but compact_context (if
        # called mid-batch) needs the ORIGINAL position, not whatever
        # self.messages[-1] happens to be after earlier tool replies in this
        # same batch have already been appended.
        assistant_idx = len(self.messages) - 1
        for tc in tool_calls:
            if self.cancel.is_set():
                raise Cancelled()
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"] or "{}"
            try:
                args = json.loads(raw_args)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as e:
                self._tool_reply(tc, f"ERROR: could not parse tool arguments: {e}. "
                                     f"Raw arguments were: {raw_args[:500]}",
                                 error=True, name=name, args={})
                continue

            # A stable per-call token: the UI carries it on the tool box so a
            # Stop click can name exactly which running command to kill, and
            # run_powershell registers its process under it (see tools.py).
            # Freshly generated (not the model's tc["id"]) so it's unique even
            # across parallel chats.
            run_token = uuid.uuid4().hex[:12]
            self.events.tool_call(name, args, call_id=run_token)

            decision = self.permissions.check(name, args, self.events.ask_permission)
            if not decision.allowed:
                msg = "User denied permission for this tool call."
                if decision.feedback:
                    msg += f" User says: {decision.feedback}"
                msg += " Do not retry it as-is; adjust your approach."
                self._tool_reply(tc, msg, error=True, name=name, args=args)
                continue

            # Verify-nudge bookkeeping: attempting a verification tool counts
            # even if it fails (a failing test run still tells the model the
            # truth about its changes); an edit only counts once it succeeds.
            if name in VERIFICATION_TOOLS:
                self._turn_verified = True

            set_call_token(run_token)
            try:
                if name == DISPATCH_WORKER_TOOL:
                    output = self._dispatch_worker(args.get("name", ""),
                                                   args.get("task", ""))
                elif name == CHECK_WORKERS_TOOL:
                    output = self._check_workers()
                elif name == STEER_WORKER_TOOL:
                    output = self._steer_worker_tool(args.get("worker", ""),
                                                     args.get("message", ""))
                elif name == STOP_WORKER_TOOL:
                    output = self._stop_worker_tool(args.get("worker", ""))
                elif name == WORKER_CHANGES_TOOL:
                    output = self._worker_changes_tool(args.get("worker", ""))
                elif name == REVERT_WORKER_TOOL:
                    output = self._revert_worker_tool(args.get("worker", ""))
                elif name == SUBAGENT_TOOL:
                    if not self.allow_subagents:
                        raise ToolError("sub-agents cannot spawn further sub-agents")
                    output = self._run_subagents(args.get("agents", []))
                elif name == CONTROL_CHROME_TOOL:
                    output = self._control_chrome_tool(
                        args.get("goal", ""), args.get("start_url", ""))
                elif name in BROWSER_ACTION_TOOLS:
                    output = self._browser_action(name, args)
                elif name == VIEW_IMAGE_TOOL:
                    output = self._view_image(args.get("path", ""), args.get("question", ""))
                elif name == GENERATE_IMAGE_TOOL:
                    output = self._generate_image(args.get("prompt", ""), args.get("path", ""),
                                                  args.get("steps", 1))
                elif name == SHOW_IMAGE_TOOL:
                    output = self._show_image_tool(args.get("path", ""), args.get("caption", ""))
                elif name == SHOW_HTTP_CAT_TOOL:
                    output = self._show_http_cat_tool(args.get("status_code", 0))
                elif name == PREVIEW_PAGE_TOOL:
                    output = self._preview_page_tool(args.get("url", ""), args.get("wait_seconds", 2.0))
                elif name == CHECK_PAGE_TOOL:
                    output = self._check_page_tool(args.get("url", ""), args.get("wait_seconds", 2.5))
                elif name == COMPACT_CONTEXT_TOOL:
                    output = self._compact_context_tool(args.get("reason", ""), assistant_idx)
                elif name == SPEAK_TOOL:
                    output = self._speak_tool(args.get("text", ""), args.get("path", ""),
                                              args.get("voice", ""), args.get("speed"))
                elif name == REMEMBER_TOOL:
                    output = execute_tool(name, args)
                    # Reflect the new memory in THIS conversation immediately,
                    # not just in future sessions (which pick it up naturally
                    # since it's read from disk on every fresh Agent init).
                    self.rebuild_system_prompt()
                elif name == REVIEW_CHANGES_TOOL:
                    output = self._review_changes_tool()
                elif name == "todo_write":
                    # Handled here (not via the module-global in tools.py) so
                    # each chat keeps its OWN checklist -- parallel chats
                    # otherwise scribble over one shared list.
                    self.todos = clean_todo_items(args.get("todos", []))
                    done = sum(1 for t in self.todos if t["status"] == "completed")
                    output = f"Todo list updated: {done}/{len(self.todos)} completed."
                elif self.mcp is not None and self.mcp.owns(name):
                    output = self.mcp.call(name, args)
                else:
                    output = execute_tool(name, args)
                if name in EDIT_TOOLS:
                    self._turn_wrote_files = True
                    self._refine_pass_changed = True  # a review pass that edits keeps Max going
                self._tool_reply(tc, output, name=name, args=args)
            except ToolError as e:
                self._tool_reply(tc, f"ERROR: {e}", error=True, name=name, args=args)
            except Exception as e:
                self._tool_reply(tc, f"ERROR: unexpected {type(e).__name__}: {e}",
                                 error=True, name=name, args=args)
            finally:
                set_call_token(None)

            if name == "todo_write":
                self.events.todos(self.todos)

    def _tool_reply(self, tc: dict, content: str, error: bool = False,
                    name: str = "", args: dict | None = None) -> None:
        self.events.tool_result(name, content, is_error=error)
        if self.transcript:
            self.transcript.tool_result(name, content, is_error=error)
        self.messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": content,
        })

    # ------------------------------------------------------------------ #
    # Sub-agents (parallel delegation)

    def _emit_subagent(self, *args, **kwargs) -> None:
        # evaluate_js isn't safe to call from several threads at once, so
        # serialize progress emits coming from the worker threads.
        with self._emit_lock:
            self.events.subagent(*args, **kwargs)

    def _emit_subagent_stream(self, aid: str, kind: str, **data) -> None:
        with self._emit_lock:
            self.events.subagent_stream(aid, kind, **data)

    def _run_subagents(self, specs: list) -> str:
        """Run each spec as an autonomous agent on its own thread, in parallel,
        and return a combined report for the coordinating model."""
        if not isinstance(specs, list) or not specs:
            raise ToolError("spawn_agents needs a non-empty 'agents' list")
        specs = specs[:MAX_SUBAGENTS]
        results: list = [None] * len(specs)
        # One shared limiter for every sub-agent spawned by this call -- the
        # free tier is rate-limited to ~1 req/s, and up to MAX_SUBAGENTS
        # threads each making their own uncoordinated requests otherwise
        # collide, burning retries/step-budget on 429 backoff instead of
        # actual task progress.
        limiter = RateLimiter()
        # Unique per CALL, not just per index within it: the frontend's
        # sub-agent inspector (threads/tabs) is keyed by id and never cleared
        # between separate spawn_agents calls in the same chat (only on
        # session switch). Plain "sa1"/"sa2" reused across a second call
        # would make its live thread/tab reuse -- and get its new stream
        # events silently appended into -- the first call's already-finished
        # DOM, showing stale or mixed content instead of the new run.
        call_id = uuid.uuid4().hex[:6]

        def worker(i: int, spec: dict) -> None:
            name = str(spec.get("name") or f"agent-{i + 1}").strip()[:60] or f"agent-{i + 1}"
            task = str(spec.get("task") or "").strip()
            aid = f"sa{call_id}-{i + 1}"
            self._emit_subagent(aid, name, "running", mission=task[:280])
            if not task:
                results[i] = (name, "", "no task was given", None)
                self._emit_subagent(aid, name, "error", summary="no task given")
                return
            try:
                report, usage = self._run_single_subagent(name, task, limiter, aid)
                results[i] = (name, report, None, usage)
                self._emit_subagent(aid, name, "done", summary=_first_line(report))
            except Exception as e:  # keep one failure from sinking the rest
                err = f"{type(e).__name__}: {e}"
                results[i] = (name, "", err, None)
                self._emit_subagent(aid, name, "error", summary=err[:280])

        threads = []
        for i, spec in enumerate(specs):
            if not isinstance(spec, dict):
                spec = {"name": f"agent-{i + 1}", "task": str(spec)}
            t = threading.Thread(target=worker, args=(i, spec), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        out = [f"Ran {len(specs)} sub-agent(s) in parallel. Their reports:\n"]
        for entry in results:
            name, report, err, usage = entry
            # Sub-agent token usage used to be dropped entirely (it lived on
            # the discarded sub-Agent). Fold it in here, after join(), so the
            # accumulation happens on one thread -- Usage.add isn't locked.
            if usage is not None:
                self.session_usage.add(usage)
            if err:
                out.append(f"### {name} — FAILED\n{err}\n")
            else:
                out.append(f"### {name}\n{report or '(no output)'}\n")
        return "\n".join(out)

    def _run_single_subagent(self, name: str, task: str, limiter: RateLimiter,
                             aid: str) -> tuple[str, Usage]:
        """One sub-agent: a fresh Agent (own client + non-interactive sink) that
        runs its mission to completion. Returns (final report text, its token
        usage) -- the caller folds the usage into the coordinator's totals."""
        # A separate client per thread avoids sharing one requests.Session
        # across concurrent requests; the rate limiter IS shared (see
        # _run_subagents) so all sub-agents' requests stay spaced out.
        client = ZaiClient(self.client.api_key, self.client.base_url, rate_limiter=limiter)
        # In voice mode a worker's gated action is approved out loud (see
        # _worker_ask) instead of auto-denied, so hands-free work isn't stuck in
        # 'ask' mode. Non-conversational sub-agents keep the auto-deny default.
        ask = (lambda title, preview, always: self._worker_ask(aid, title, preview, always)) \
            if self.conversational else None
        sink = _CaptureEvents(forward=self._emit_subagent_stream, aid=aid, ask=ask)
        sub = Agent(self.cfg, client, events=sink, allow_subagents=False,
                    workdir=self.workdir)
        # Same work-tree, same pre-turn baseline -- so review_changes works
        # inside sub-agents too.
        sub.backup_repo = self.backup_repo
        # MCP servers are process-global external processes; sub-agents can
        # use their tools the same way the coordinator can.
        sub.mcp = self.mcp
        # Sub-agents inherit the chat's model (their client already points at
        # the same provider via self.client's key/url above). vision_client is
        # NOT shared -- it wraps a requests.Session, which isn't safe across
        # worker threads; sub-agent vision falls back to their own client.
        sub.model_override = self.model_override
        with self._active_subagents_lock:
            self._active_subagents[aid] = sub
        try:
            sub.run_turn({"role": "user",
                          "content": SUBAGENT_PREAMBLE.format(name=name, task=task)})
        finally:
            with self._active_subagents_lock:
                self._active_subagents.pop(aid, None)
        report = _final_report_text(sub.messages)
        if report:
            return report, sub.session_usage
        for m in reversed(sub.messages):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) \
                    and m["content"].strip():
                return m["content"].strip(), sub.session_usage
        if sink.text.strip():
            return sink.text.strip(), sub.session_usage
        # No report by any path means the sub-agent's turn ended without ever
        # producing text -- almost always because it died early (an ApiError
        # like a rate-limit, which _run_turn catches and swallows without
        # appending any assistant message). Previously this returned a bland
        # placeholder string that the worker then reported as a normal "done"
        # -- so a sub-agent that actually FAILED showed up as finished-with-
        # no-report. Raise instead, so the worker marks it an error with the
        # real reason.
        raise ToolError(sink.last_error
                        or "sub-agent ended without producing any output")

    # ------------------------------------------------------------------ #
    # Fire-and-forget background workers (conversational mode)

    def _dispatch_worker(self, name: str, task: str) -> str:
        """Start a background worker on its own daemon thread and return
        IMMEDIATELY (never joins). The worker runs a full autonomous sub-agent;
        it reports progress through worker_update events, and its result lands
        in the registry for check_workers to read. This is what keeps the
        conversational agent responsive: real work happens off to the side."""
        task = str(task or "").strip()
        if not task:
            raise ToolError("dispatch_worker needs a non-empty 'task'")
        # Snapshot the project's current state as this worker's baseline, so we
        # can later show exactly what IT changed and revert just its work.
        baseline = None
        if self.backup_repo is not None:
            try:
                baseline = self.backup_repo.snapshot(f"before worker: {name}")
            except Exception:
                baseline = None
        with self._workers_lock:
            self._worker_seq += 1
            wid = f"wk{self._worker_seq}"
            name = str(name or "").strip()[:60] or f"worker-{self._worker_seq}"
            self._workers[wid] = {
                "id": wid, "name": name, "task": task, "status": "running",
                "result": "", "error": None, "baseline": baseline, "changes": [],
            }
        # One rate limiter shared by every background worker in this chat, so
        # several running at once stay spaced out on the free tier's ~1 req/s.
        if getattr(self, "_worker_limiter", None) is None:
            self._worker_limiter = RateLimiter()
        self.events.worker_update(wid, name, "started")
        self._emit_subagent(wid, name, "running", mission=task[:280])
        t = threading.Thread(target=self._run_worker, args=(wid, name, task),
                             daemon=True)
        t.start()
        return (f"Started background worker '{name}' (id {wid}). It's running now; "
                f"tell the user out loud you've started, keep talking, and don't wait "
                f"for it -- you'll be told when it finishes.")

    def _run_worker(self, wid: str, name: str, task: str) -> None:
        """Body of a background worker thread: run the mission to completion and
        record the outcome. Never raises out of the thread."""
        try:
            report, usage = self._run_single_subagent(
                name, task, self._worker_limiter, wid)
            # What did this worker actually change (vs its dispatch baseline)?
            changes = self._worker_changed_files(wid)
            # A worker the user stopped (cancelled) may still return a partial
            # report -- don't flip its "stopped" status back to "done".
            stopped = False
            with self._workers_lock:
                w = self._workers.get(wid)
                if w is not None:
                    w["changes"] = changes
                if w is not None and w["status"] == "stopped":
                    stopped = True
                elif w is not None:
                    w["status"], w["result"] = "done", report
            # Sub-agent token usage is folded in here, on the worker thread.
            # Usage.add isn't locked; guard it with the emit lock (also held by
            # every events emit) so we don't race the main turn's accounting.
            with self._emit_lock:
                if usage is not None:
                    self.session_usage.add(usage)
            if not stopped:
                self._emit_subagent(wid, name, "done", summary=_first_line(report))
                # Fold the concrete file changes into the result so the spoken
                # announcement can mention what actually changed.
                ann = report
                if changes:
                    ann = f"{report}\n\nFiles changed: {self._describe_changes(changes)}."
                self.events.worker_update(wid, name, "done",
                                          summary=_first_line(report), result=ann)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            with self._workers_lock:
                w = self._workers.get(wid)
                if w is not None and w["status"] != "stopped":
                    w["status"], w["error"] = "error", err
            self._emit_subagent(wid, name, "error", summary=err[:280])
            self.events.worker_update(wid, name, "error", summary=err[:280], result=err)

    def _resolve_worker(self, ident: str) -> str | None:
        """Map a spoken/typed worker reference ('wk1', or a name like 'dark
        mode') to a worker id. Prefers a running match. Returns None if nothing
        matches."""
        ident = str(ident or "").strip().lower()
        if not ident:
            return None
        with self._workers_lock:
            workers = list(self._workers.values())
        # Exact id first.
        for w in workers:
            if w["id"].lower() == ident:
                return w["id"]
        # Then by name (prefer running), exact-ish then loose contains.
        def name_hit(w):
            nm = w["name"].lower()
            return ident == nm or ident in nm or nm in ident
        running = [w for w in workers if w["status"] == "running" and name_hit(w)]
        if running:
            return running[0]["id"]
        any_hit = [w for w in workers if name_hit(w)]
        return any_hit[0]["id"] if any_hit else None

    def _steer_worker_tool(self, ident: str, message: str) -> str:
        message = str(message or "").strip()
        if not message:
            raise ToolError("steer_worker needs a non-empty 'message'")
        wid = self._resolve_worker(ident)
        if wid is None:
            raise ToolError(f"No worker matches '{ident}'. Use check_workers to see them.")
        with self._workers_lock:
            name = self._workers.get(wid, {}).get("name", wid)
            status = self._workers.get(wid, {}).get("status")
        if status != "running":
            return f"Worker '{name}' isn't running anymore, so there's nothing to steer."
        if self.steer_subagent(wid, message):
            return f"Passed that along to '{name}'."
        return (f"'{name}' already has a queued instruction it hasn't picked up yet; "
                f"try again in a moment.")

    def _stop_worker_tool(self, ident: str) -> str:
        wid = self._resolve_worker(ident)
        if wid is None:
            raise ToolError(f"No worker matches '{ident}'. Use check_workers to see them.")
        with self._workers_lock:
            w = self._workers.get(wid, {})
            name = w.get("name", wid)
            status = w.get("status")
        if status != "running":
            return f"Worker '{name}' has already finished."
        # Cancel its in-flight run; the worker thread marks the registry.
        with self._active_subagents_lock:
            sub = self._active_subagents.get(wid)
        if sub is not None:
            sub.request_cancel()
        with self._workers_lock:
            if wid in self._workers and self._workers[wid]["status"] == "running":
                self._workers[wid]["status"] = "stopped"
        self._emit_subagent(wid, name, "error", summary="stopped by user")
        return f"Stopping '{name}'."

    def _worker_changed_files(self, wid: str) -> list:
        """(status, path) pairs for what a worker changed vs its baseline."""
        with self._workers_lock:
            baseline = self._workers.get(wid, {}).get("baseline")
        if not baseline or self.backup_repo is None:
            return []
        try:
            return self.backup_repo.changed_files_since(baseline)
        except Exception:
            return []

    @staticmethod
    def _describe_changes(changes: list) -> str:
        if not changes:
            return "no file changes"
        verb = {"A": "added", "M": "changed", "D": "deleted", "R": "renamed"}
        parts = [f"{verb.get(st, 'touched')} {path}" for st, path in changes[:12]]
        extra = len(changes) - 12
        if extra > 0:
            parts.append(f"and {extra} more")
        return "; ".join(parts)

    def _worker_changes_tool(self, ident: str) -> str:
        wid = self._resolve_worker(ident)
        if wid is None:
            raise ToolError(f"No worker matches '{ident}'. Use check_workers to see them.")
        with self._workers_lock:
            w = self._workers.get(wid, {})
            name, changes = w.get("name", wid), w.get("changes", [])
        if not changes:
            return f"'{name}' didn't change any project files."
        return (f"'{name}' changed {len(changes)} file(s): "
                f"{self._describe_changes(changes)}.")

    def _revert_worker_tool(self, ident: str) -> str:
        wid = self._resolve_worker(ident)
        if wid is None:
            raise ToolError(f"No worker matches '{ident}'. Use check_workers to see them.")
        with self._workers_lock:
            w = self._workers.get(wid, {})
            name, baseline, changes = w.get("name", wid), w.get("baseline"), w.get("changes", [])
        if self.backup_repo is None or not baseline:
            return (f"I can't revert '{name}' -- backups aren't on for this chat, so "
                    f"there's no snapshot to roll back to.")
        if not changes:
            return f"'{name}' didn't change any files, so there's nothing to revert."
        try:
            self.backup_repo.revert_to(baseline)
        except Exception as e:
            raise ToolError(f"Could not revert '{name}': {e}")
        with self._workers_lock:
            if wid in self._workers:
                self._workers[wid]["status"] = "reverted"
        self._emit_subagent(wid, name, "error", summary="reverted by user")
        return (f"Reverted the project to before '{name}' ran, undoing its changes "
                f"({self._describe_changes(changes)}). Note: this also rolls back any "
                f"changes other workers made after '{name}' started.")

    def _worker_ask(self, wid: str, title: str, preview: str, always_label):
        """Called ON A WORKER THREAD when that worker hits a permission-gated
        action in voice mode. Surfaces the request to the user (spoken + an
        overlay card) and BLOCKS until they answer -- so hands-free work isn't
        stuck in 'ask' mode. Returns 'y' | 'a' | ('n', feedback)."""
        with self._workers_lock:
            name = self._workers.get(wid, {}).get("name", wid)
        rid = uuid.uuid4().hex
        entry = {"event": threading.Event(), "answer": ("n", "")}
        with self._worker_perms_lock:
            self._worker_perms[rid] = entry
        # Speak a short, plain-language version and show the full detail on-card.
        spoken = f"The {name} task wants to {_spoken_permission(title)}. Should I let it?"
        try:
            self.events.worker_permission(rid, name, title, preview,
                                          spoken=spoken, always=always_label or "")
        except Exception:
            pass
        # Block this worker until answered (or a long timeout, then deny).
        answered = entry["event"].wait(timeout=300)
        with self._worker_perms_lock:
            self._worker_perms.pop(rid, None)
        if not answered:
            return ("n", "no answer from the user; skipped for now")
        return entry["answer"]

    def resolve_worker_permission(self, rid: str, answer, feedback: str = "") -> bool:
        """Deliver the user's answer ('y'|'a'|'n') to a blocked worker. Returns
        False if that request is unknown/expired."""
        with self._worker_perms_lock:
            entry = self._worker_perms.get(rid)
        if not entry:
            return False
        entry["answer"] = ("n", feedback or "") if answer == "n" else answer
        entry["event"].set()
        return True

    def pending_worker_permission(self) -> bool:
        with self._worker_perms_lock:
            return bool(self._worker_perms)

    def deny_pending_worker_permissions(self, feedback: str = "") -> None:
        """Release every worker currently blocked on a permission prompt with a
        denial -- so closing voice mode doesn't leave workers hung forever."""
        with self._worker_perms_lock:
            entries = list(self._worker_perms.values())
        for entry in entries:
            entry["answer"] = ("n", feedback or "voice mode was closed")
            entry["event"].set()

    def _check_workers(self) -> str:
        """A plain-text roundup of every worker dispatched this chat, for the
        conversational agent to answer 'how's it going?' from."""
        with self._workers_lock:
            workers = list(self._workers.values())
        if not workers:
            return "No background workers have been dispatched yet."
        running = [w for w in workers if w["status"] == "running"]
        done = [w for w in workers if w["status"] == "done"]
        errored = [w for w in workers if w["status"] == "error"]
        lines = [f"{len(running)} running, {len(done)} done, {len(errored)} failed.\n"]
        for w in workers:
            if w["status"] == "running":
                lines.append(f"- {w['name']} ({w['id']}): still working — {w['task'][:120]}")
            elif w["status"] == "done":
                lines.append(f"- {w['name']} ({w['id']}): DONE — {_first_line(w['result'])}")
            elif w["status"] == "stopped":
                lines.append(f"- {w['name']} ({w['id']}): STOPPED by the user")
            else:
                lines.append(f"- {w['name']} ({w['id']}): FAILED — {w['error']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Interactive browser (control_chrome)

    def _ensure_browser_session(self):
        """The chat's persistent BrowserSession, created on first use. Kept on
        the coordinator so cookies/login/page survive across control_chrome
        calls; sub-agents share this exact instance."""
        from .browser_session import BrowserSession
        sess = self.browser_session
        if sess is None or not sess.is_open:
            headless = bool(getattr(self.cfg, "browser_headless", False))
            # Opt-in persistent profile: a dedicated agent profile directory
            # (NEVER the user's own browser) whose logins survive restarts.
            user_data_dir = None
            if getattr(self.cfg, "browser_keep_logins", False):
                from .config import CONFIG_DIR
                user_data_dir = str(CONFIG_DIR / "browser-profile")
            sess = BrowserSession(headless=headless, status=self.events.info,
                                  user_data_dir=user_data_dir)
            sess.start()  # raises here (surfaced to the model) if launch fails
            self.browser_session = sess
        return sess

    def close_browser(self) -> None:
        """Tear down the chat's browser, if any. Called when the chat closes."""
        sess = self.browser_session
        self.browser_session = None
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass

    def _control_chrome_tool(self, goal: str, start_url: str = "") -> str:
        """Spawn a specialized Browser Agent that drives the chat's persistent
        browser toward `goal`, then return its report. The browser itself
        outlives the sub-agent (see _ensure_browser_session)."""
        goal = (goal or "").strip()
        if not goal:
            raise ToolError("control_chrome needs a 'goal'.")
        if not self.allow_subagents:
            raise ToolError("a sub-agent cannot itself launch a browser agent")
        try:
            session = self._ensure_browser_session()
        except Exception as e:
            raise ToolError(
                f"Could not start the browser: {e}. Playwright + Chromium install "
                "on first use and need network access once.")
        aid = f"chrome-{uuid.uuid4().hex[:6]}"
        self._emit_subagent(aid, "browser", "running", mission=goal[:280])
        prompt = goal
        if start_url.strip():
            prompt = f"{goal}\n\n(Start by navigating to: {start_url.strip()})"
        try:
            report = self._run_browser_subagent(prompt, session, aid)
            self._emit_subagent(aid, "browser", "done", summary=_first_line(report))
            return report
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            self._emit_subagent(aid, "browser", "error", summary=err[:280])
            raise ToolError(f"Browser agent failed: {e}")

    def _browser_client_and_model(self):
        """Client + model_override for the Browser Agent. When Settings names
        a dedicated browser model (typically something stronger than the free
        flash model -- driving a page is the hardest thing a small model does
        here), resolve its provider and use that; otherwise inherit the
        chat's own client/model."""
        name = (getattr(self.cfg, "browser_provider", "") or "").strip()
        model = (getattr(self.cfg, "browser_model", "") or "").strip()
        if name and model:
            from .config import find_provider
            prov = find_provider(self.cfg, name)
            if prov is not None:
                if prov.get("builtin"):
                    key = self.cfg.resolve_api_key()
                    if key:
                        return ZaiClient(key, self.cfg.base_url), None
                else:
                    return (ZaiClient(prov.get("api_key", ""), prov["base_url"]),
                            model)
            self.events.warn(f"browser model '{model}' ({name}) not found -- "
                             "using the chat's model instead")
        return (ZaiClient(self.client.api_key, self.client.base_url),
                self.model_override)

    def _run_browser_subagent(self, goal: str, session, aid: str) -> str:
        """A sub-agent whose ONLY tools are the browser_* actions and whose
        system prompt is the Browser Agent prompt. Shares the given
        BrowserSession so it drives the chat's live browser."""
        client, model_override = self._browser_client_and_model()
        sink = _CaptureEvents(forward=self._emit_subagent_stream, aid=aid)
        sub = Agent(self.cfg, client, events=sink, allow_subagents=False,
                    workdir=self.workdir)
        sub.model_override = model_override
        sub.browser_session = session
        sub.pausable = True  # the human can pause it and take over the browser
        # Restrict the sub-agent to ONLY the browser action tools, and give it
        # the specialized browser system prompt in place of the coding one.
        sub.tool_schemas = list(BROWSER_AGENT_SCHEMAS)
        sub._base_system_prompt = BROWSER_AGENT_SYSTEM.format(goal=goal)
        with self._active_subagents_lock:
            self._active_subagents[aid] = sub
        self._browser_agent_aid = aid
        try:
            sub.run_turn({"role": "user", "content": "Begin. Work toward the goal, "
                          "one action at a time, and report when done or blocked."})
        finally:
            with self._active_subagents_lock:
                self._active_subagents.pop(aid, None)
            self._browser_agent_aid = None
        report = _final_report_text(sub.messages) or sink.text.strip()
        if not report:
            raise ToolError(sink.last_error
                            or "browser agent ended without producing a report")
        return report

    # Browser actions that change what's on screen -> push a live frame after.
    _BROWSER_STATE_CHANGING = {"browser_navigate", "browser_click", "browser_click_at",
                               "browser_type", "browser_key"}

    def _browser_action(self, name: str, args: dict) -> str:
        """Dispatch a browser_* tool (only ever called inside a Browser Agent,
        which has self.browser_session set)."""
        session = self.browser_session
        if session is None or not session.is_open:
            raise ToolError("The browser is not open.")
        try:
            if name == "browser_navigate":
                return session.navigate(args.get("url", ""))
            if name == "browser_snapshot":
                return session.snapshot()
            if name == "browser_click":
                return session.click(args.get("ref"))
            if name == "browser_click_at":
                return session.click_at(args.get("x"), args.get("y"))
            if name == "browser_type":
                return session.type_text(args.get("ref"), args.get("text", ""),
                                         bool(args.get("submit", False)))
            if name == "browser_key":
                return session.press(args.get("key", ""))
            if name == "browser_read":
                return session.read_text()
            if name == "browser_wait":
                return session.wait(args.get("seconds", 2.0))
            if name == "browser_screenshot":
                out = self.workdir / "generated" / f"browser-{uuid.uuid4().hex[:8]}.png"
                path = session.screenshot(out)
                return self._view_image(path, args.get("question", ""))
            raise ToolError(f"unknown browser action {name}")
        finally:
            if name in self._BROWSER_STATE_CHANGING:
                self._emit_browser_frame(session)

    def _emit_browser_frame(self, session) -> None:
        """Push a small live screenshot of the page to the UI's Browser panel.
        Best-effort: a failed frame never disrupts the browsing itself."""
        try:
            image = session.screenshot_b64()
            if image:
                self.events.browser_frame(url=session.current_url(), image=image)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Context management

    def context_estimate(self) -> int:
        return estimate_tokens(self.messages)

    def maybe_autocompact(self) -> None:
        if self.context_estimate() > self.cfg.context_limit_tokens:
            self.events.warn("context is getting large; compacting older history...")
            self.compact()

    def compact(self) -> str:
        """Summarize the conversation and restart the context from the summary."""
        if len(self.messages) < 4:
            return "Nothing to compact yet."
        transcript = self.messages[1:]  # skip system prompt
        compact_model = (self.cfg.vision_model if self._payload_has_images()
                         else (self.model_override or self.cfg.model))
        with self.events.status("compacting conversation..."):
            result = self._client_for(compact_model).chat(
                model=compact_model,
                messages=transcript + [{"role": "user", "content": COMPACT_PROMPT}],
                tools=None,
                temperature=0.3,
                max_tokens=4096,
                thinking=False,
            )
        summary = result.content.strip()
        self.session_usage.add(result.usage)
        self.events.compacted(summary)
        if self.transcript:
            self.transcript.marker(
                "Context compacted here -- everything above this line is no "
                "longer in the model's context (this transcript keeps it all)")
        # A summary is lossy by definition -- when a transcript exists, tell
        # the model exactly where the details it just lost can be found.
        details_note = ""
        if self.transcript:
            details_note = (f"\n\n[The FULL pre-compaction conversation is preserved at "
                            f"{self.transcript.path} -- grep/read it if you need any "
                            f"detail this summary leaves out.]")
        self.messages = [self.messages[0], {
            "role": "user",
            "content": ("[Context was compacted. Summary of the session so far:]\n\n"
                        + summary + details_note +
                        "\n\n[Continue helping the user from this state.]"),
        }, {
            "role": "assistant",
            "content": "Understood — I have the session summary and will continue from there.",
        }]
        return f"Compacted to ~{self.context_estimate():,} tokens."

    def _compact_context_tool(self, reason: str, assistant_idx: int) -> str:
        """The model's own compact_context tool call. Runs mid-turn, while
        self.messages[assistant_idx] is the assistant turn currently being
        processed (it has pending tool_calls of its own, possibly including
        earlier ones in this same batch whose replies are already appended
        after it). compact() would otherwise wipe that in-flight turn along
        with everything else -- instead, summarize everything BEFORE it, then
        reattach it (and any sibling tool replies already appended for this
        batch) on top of the fresh summary, so the conversation stays valid
        for whatever tool replies get appended after this one returns."""
        if assistant_idx < 4:
            return "Nothing to compact yet."
        in_flight = self.messages[assistant_idx:]
        self.messages = self.messages[:assistant_idx]
        note = self.compact()
        self.messages.extend(in_flight)
        return note + (f" (reason: {reason})" if reason else "")
