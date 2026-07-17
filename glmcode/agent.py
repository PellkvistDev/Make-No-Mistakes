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
from .prompts import (COMPACT_PROMPT, CONTINUE_NUDGE, STEER_NUDGE_TEMPLATE,
                      STEP_LIMIT_NUDGE, SUBAGENT_PREAMBLE, VERIFY_NUDGE,
                      VIEW_IMAGE_PROMPT, VISION_ANALYSIS_PROMPT, WRAP_UP_NUDGE,
                      build_system_prompt)
from .tools import (COMPACT_CONTEXT_TOOL, GENERATE_IMAGE_TOOL, PREVIEW_PAGE_TOOL,
                    REMEMBER_TOOL, REVIEW_CHANGES_TOOL, SHOW_HTTP_CAT_TOOL,
                    SHOW_IMAGE_TOOL, SPEAK_TOOL, SUBAGENT_TOOL, TOOL_SCHEMAS,
                    VIEW_IMAGE_TOOL, ToolError, clean_todo_items, execute_tool,
                    set_call_token, set_workdir)

# Tools whose output tells the model whether its changes actually work --
# used by the verify-nudge (see _run_turn): a turn that edits files but never
# runs any of these gets one automatic push to verify before finishing.
VERIFICATION_TOOLS = {"run_powershell", "run_background", "run_tests",
                      "run_test_file", "preview_page"}
EDIT_TOOLS = {"write_file", "edit_file"}

MAX_SUBAGENTS = 6
# Safety cap on auto-continue-on-truncation rounds (see _call_model_until_done).
MAX_CONTINUATIONS = 3


def _first_line(text: str, limit: int = 280) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


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

    def __init__(self, forward, aid: str):
        self.text = ""
        self.last_error = ""  # last error()'d message, for the report fallback
        self._forward = forward
        self._aid = aid

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

    def info(self, msg: str) -> None:
        self._forward(self._aid, "notice", level="info", text=msg)

    def warn(self, msg: str) -> None:
        self._forward(self._aid, "notice", level="warn", text=msg)

    def error(self, msg: str) -> None:
        self.last_error = msg
        self._forward(self._aid, "notice", level="error", text=msg)


class Agent:
    def __init__(self, cfg: Config, client: ZaiClient, events: AgentEvents | None = None,
                 allow_subagents: bool = True, workdir: Path | None = None):
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
        self.permissions = PermissionEngine(mode=cfg.mode)
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
        self.tool_schemas = TOOL_SCHEMAS if allow_subagents else [
            s for s in TOOL_SCHEMAS if s["function"]["name"] != SUBAGENT_TOOL
        ]
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
        # Verify-nudge bookkeeping, reset each turn (see _run_turn).
        self._turn_wrote_files = False
        self._turn_verified = False
        self._verify_nudged = False
        # Per-agent todo list (todo_write is handled in-dispatch): parallel
        # chats each keep their own checklist instead of sharing one global.
        self.todos: list[dict] = []
        self.rebuild_system_prompt()

    # ------------------------------------------------------------------ #

    def rebuild_system_prompt(self) -> None:
        # Cached separately from the live message so refreshing the context-
        # usage note (see _refresh_context_note) doesn't need to re-run
        # build_system_prompt's git subprocess calls on every model call.
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

    def attach_files(self, text: str, paths: list[Path]) -> dict:
        """Build the user message for a turn with attached files (any type,
        not just images). Unlike attach_images, nothing is read, encoded, or
        sent to any model here -- each file is copied into an uploads/
        folder in the project and the model gets a path reference, the same
        way it would find any other file already in the project. It decides
        for itself whether to read_file/view_image the attachment."""
        refs = []
        for p in paths:
            dest = self.workdir / "uploads" / f"{p.stem}-{uuid.uuid4().hex[:6]}{p.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(p, dest)
                refs.append(f"{p.name} (see {self._display_path(dest)})")
            except OSError as e:
                refs.append(f"{p.name} (FAILED to attach: {e})")

        note = ("The user attached a file: " if len(refs) == 1 else
                "The user attached files: ") + ", ".join(refs)
        combined = f"{text}\n\n[{note}]" if text else f"[{note}]"
        return {"role": "user", "content": combined}

    def _payload_has_images(self) -> bool:
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list) and any(
                part.get("type") == "image_url" for part in c
            ):
                return True
        return False

    @staticmethod
    def _resolve_existing_image(path: str, tool_name: str) -> Path:
        """Resolve+validate a path argument that must point at an existing,
        supported image file. Shared by view_image and show_image."""
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

    @staticmethod
    def _display_path(p: Path) -> str:
        """cwd-relative path for a nicer/portable tool-result marker, when possible."""
        try:
            return str(p.relative_to(self.workdir))
        except ValueError:
            return str(p)

    def _view_image(self, path: str, question: str = "") -> str:
        """The agent's own tool for looking at an image file (as opposed to
        attach_images, which handles an image the user attached)."""
        p = self._resolve_existing_image(path, "view_image")
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

    @staticmethod
    def _asset_marker(kind: str, p: Path, caption: str, note: str) -> str:
        """Tool-result text carrying a machine-parseable marker so sessions.py
        can reconstruct an inline image/audio card when a session is reopened
        later (see sessions.to_display / _extract_asset_marker)."""
        marker = f"[{kind}: {Agent._display_path(p)}]"
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
        """Generate speech locally with Kokoro and play it for the user.
        Defaults to the user's configured Settings voice/speed (the same
        ones the read-aloud toggle uses) unless the model explicitly asks
        for something different."""
        from .tts import DEFAULT_VOICE, list_voices, save_wav
        text = (text or "").strip()
        if not text:
            raise ToolError("speak needs a 'text'")
        requested_voice = (voice or "").strip()
        if requested_voice:
            # An invalid id would otherwise be silently swapped for
            # DEFAULT_VOICE inside synthesize() -- correct but confusing,
            # since the model never finds out its request didn't apply.
            # Only validate an EXPLICIT request; the user's own configured
            # voice (the no-argument default below) is trusted as-is.
            valid = list_voices()
            if requested_voice not in valid:
                raise ToolError(
                    f"'{requested_voice}' is not a valid voice id. Valid ids: "
                    f"{', '.join(valid)}"
                )
        voice = requested_voice or self.cfg.tts_voice or DEFAULT_VOICE
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
                saved = save_wav(text, out_path, voice=voice, speed=speed, status=self.events.info)
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

        model = self.model_override or self.cfg.model
        if self._payload_has_images():
            model = self.cfg.vision_model
            self.events.info(f"images in context -> routing to {model}")

        for iteration in range(self.cfg.max_turns_per_request):
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
                # One automatic push before accepting a final answer: a turn
                # that edited files but never ran ANYTHING has skipped the
                # "Verify" step entirely -- ask once, then accept whatever
                # the model decides (it may legitimately decline).
                if (self._turn_wrote_files and not self._turn_verified
                        and not self._verify_nudged):
                    self._verify_nudged = True
                    self.events.info("files were edited but nothing was run -- "
                                     "asking the agent to verify its changes")
                    self.messages.append({"role": "user", "content": VERIFY_NUDGE})
                    continue
                return  # final answer already streamed

            try:
                self._handle_tool_calls(result.tool_calls)
            except (Cancelled, KeyboardInterrupt):
                self.events.warn("interrupted during tool execution")
                return

            self._inject_steer_messages()

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
                tools=self.tool_schemas,
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
                thinking=self.cfg.thinking,
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
                if name == SUBAGENT_TOOL:
                    if not self.allow_subagents:
                        raise ToolError("sub-agents cannot spawn further sub-agents")
                    output = self._run_subagents(args.get("agents", []))
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
                else:
                    output = execute_tool(name, args)
                if name in EDIT_TOOLS:
                    self._turn_wrote_files = True
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
        sink = _CaptureEvents(forward=self._emit_subagent_stream, aid=aid)
        sub = Agent(self.cfg, client, events=sink, allow_subagents=False,
                    workdir=self.workdir)
        # Same work-tree, same pre-turn baseline -- so review_changes works
        # inside sub-agents too.
        sub.backup_repo = self.backup_repo
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
