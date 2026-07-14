"""The agentic loop: model <-> tools until the task is done.

Frontend-agnostic: all rendering and permission prompts go through an
AgentEvents sink (terminal: ui.ConsoleEvents, desktop app: gui.WebEvents).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .api import ApiError, Cancelled, Usage, ZaiClient, estimate_tokens
from .config import Config
from .events import AgentEvents
from .permissions import PermissionEngine
from .prompts import (COMPACT_PROMPT, SUBAGENT_PREAMBLE, VIEW_IMAGE_PROMPT,
                      VISION_ANALYSIS_PROMPT, build_system_prompt)
from .tools import (SUBAGENT_TOOL, TOOL_SCHEMAS, VIEW_IMAGE_TOOL, ToolError,
                    execute_tool, get_todos)

MAX_SUBAGENTS = 6


def _first_line(text: str, limit: int = 280) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


class _CaptureEvents(AgentEvents):
    """Non-interactive event sink for a sub-agent: captures streamed text and,
    by inheriting AgentEvents, auto-denies any permission prompt (so a sub-agent
    can only do what the current mode allows without asking)."""

    def __init__(self):
        self.text = ""

    def content_delta(self, text: str) -> None:
        self.text += text


class Agent:
    def __init__(self, cfg: Config, client: ZaiClient, events: AgentEvents | None = None,
                 allow_subagents: bool = True):
        self.cfg = cfg
        self.client = client
        if events is None:
            from .ui import ConsoleEvents
            events = ConsoleEvents(cfg)
        self.events = events
        self.permissions = PermissionEngine(mode=cfg.mode)
        self.messages: list[dict] = []
        self.session_usage = Usage()
        self.cancel = threading.Event()
        self.busy = False
        # Sub-agents don't get the spawning tool themselves (no recursion).
        self.allow_subagents = allow_subagents
        self.tool_schemas = TOOL_SCHEMAS if allow_subagents else [
            s for s in TOOL_SCHEMAS if s["function"]["name"] != SUBAGENT_TOOL
        ]
        self._emit_lock = threading.Lock()  # serialize sub-agent progress emits
        self.rebuild_system_prompt()

    # ------------------------------------------------------------------ #

    def rebuild_system_prompt(self) -> None:
        sys_msg = {"role": "system",
                   "content": build_system_prompt(Path.cwd(), self.cfg.model)}
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = sys_msg
        else:
            self.messages.insert(0, sys_msg)

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
            analysis = self.client.analyze_images(
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

    def _payload_has_images(self) -> bool:
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list) and any(
                part.get("type") == "image_url" for part in c
            ):
                return True
        return False

    def _view_image(self, path: str, question: str = "") -> str:
        """The agent's own tool for looking at an image file (as opposed to
        attach_images, which handles an image the user attached)."""
        from .api import IMAGE_EXTENSIONS
        raw = str(path or "").strip()
        if not raw:
            raise ToolError("view_image needs a 'path'")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            raise ToolError(f"Image not found: {p}")
        if p.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ToolError(
                f"Not a supported image type ({p.suffix or '(none)'}): {p}. "
                f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}"
            )
        focus = (f"What the agent needs to know: {question.strip()}" if question and question.strip()
                 else "No specific focus was given; describe the image exhaustively.")
        prompt = VIEW_IMAGE_PROMPT.format(focus=focus)
        with self.events.status(f"looking at {p.name} with {self.cfg.vision_model}..."):
            try:
                result = self.client.analyze_images(self.cfg.vision_model, prompt, [p])
            except ValueError as e:  # e.g. encode_image_data_uri's size-limit check
                raise ToolError(str(e))
        return result.strip() or "(vision model returned no description)"

    # ------------------------------------------------------------------ #
    # Main loop

    def run_turn(self, user_message: dict) -> None:
        """One user turn: append the message, loop model+tools until done."""
        self.cancel.clear()
        self.busy = True
        try:
            self._run_turn(user_message)
        finally:
            self.busy = False
            self.events.turn_done(self.session_usage, self.context_estimate())

    def _run_turn(self, user_message: dict) -> None:
        self.maybe_autocompact()
        self.messages.append(user_message)

        model = self.cfg.model
        if self._payload_has_images():
            model = self.cfg.vision_model
            self.events.info(f"images in context -> routing to {model}")

        for iteration in range(self.cfg.max_turns_per_request):
            try:
                result = self._call_model(model)
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
                return

            self.session_usage.add(result.usage)
            self.messages.append(result.to_message())

            if not result.tool_calls:
                return  # final answer already streamed

            try:
                self._handle_tool_calls(result.tool_calls)
            except (Cancelled, KeyboardInterrupt):
                self.events.warn("interrupted during tool execution")
                return

        self.events.warn(f"stopped after {self.cfg.max_turns_per_request} agentic "
                         "steps; say 'continue' to let it keep going")

    def _call_model(self, model: str):
        self.events.stream_start()
        try:
            return self.client.chat(
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

    # ------------------------------------------------------------------ #

    def _handle_tool_calls(self, tool_calls: list) -> None:
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

            self.events.tool_call(name, args)

            decision = self.permissions.check(name, args, self.events.ask_permission)
            if not decision.allowed:
                msg = "User denied permission for this tool call."
                if decision.feedback:
                    msg += f" User says: {decision.feedback}"
                msg += " Do not retry it as-is; adjust your approach."
                self._tool_reply(tc, msg, error=True, name=name, args=args)
                continue

            try:
                if name == SUBAGENT_TOOL:
                    if not self.allow_subagents:
                        raise ToolError("sub-agents cannot spawn further sub-agents")
                    output = self._run_subagents(args.get("agents", []))
                elif name == VIEW_IMAGE_TOOL:
                    output = self._view_image(args.get("path", ""), args.get("question", ""))
                else:
                    output = execute_tool(name, args)
                self._tool_reply(tc, output, name=name, args=args)
            except ToolError as e:
                self._tool_reply(tc, f"ERROR: {e}", error=True, name=name, args=args)
            except Exception as e:
                self._tool_reply(tc, f"ERROR: unexpected {type(e).__name__}: {e}",
                                 error=True, name=name, args=args)

            if name == "todo_write":
                self.events.todos(get_todos())

    def _tool_reply(self, tc: dict, content: str, error: bool = False,
                    name: str = "", args: dict | None = None) -> None:
        self.events.tool_result(name, content, is_error=error)
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

    def _run_subagents(self, specs: list) -> str:
        """Run each spec as an autonomous agent on its own thread, in parallel,
        and return a combined report for the coordinating model."""
        if not isinstance(specs, list) or not specs:
            raise ToolError("spawn_agents needs a non-empty 'agents' list")
        specs = specs[:MAX_SUBAGENTS]
        results: list = [None] * len(specs)

        def worker(i: int, spec: dict) -> None:
            name = str(spec.get("name") or f"agent-{i + 1}").strip()[:60] or f"agent-{i + 1}"
            task = str(spec.get("task") or "").strip()
            aid = f"sa{i + 1}"
            self._emit_subagent(aid, name, "running", mission=task[:280])
            if not task:
                results[i] = (name, "", "no task was given")
                self._emit_subagent(aid, name, "error", summary="no task given")
                return
            try:
                report = self._run_single_subagent(name, task)
                results[i] = (name, report, None)
                self._emit_subagent(aid, name, "done", summary=_first_line(report))
            except Exception as e:  # keep one failure from sinking the rest
                err = f"{type(e).__name__}: {e}"
                results[i] = (name, "", err)
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
            name, report, err = entry
            if err:
                out.append(f"### {name} — FAILED\n{err}\n")
            else:
                out.append(f"### {name}\n{report or '(no output)'}\n")
        return "\n".join(out)

    def _run_single_subagent(self, name: str, task: str) -> str:
        """One sub-agent: a fresh Agent (own client + non-interactive sink) that
        runs its mission to completion and returns its final report text."""
        # A separate client per thread avoids sharing one requests.Session
        # across concurrent requests.
        client = ZaiClient(self.client.api_key, self.client.base_url)
        sink = _CaptureEvents()
        sub = Agent(self.cfg, client, events=sink, allow_subagents=False)
        sub.run_turn({"role": "user",
                      "content": SUBAGENT_PREAMBLE.format(name=name, task=task)})
        for m in reversed(sub.messages):
            if m.get("role") == "assistant" and isinstance(m.get("content"), str) \
                    and m["content"].strip():
                return m["content"].strip()
        return sink.text.strip() or "(sub-agent produced no final report)"

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
                         else self.cfg.model)
        with self.events.status("compacting conversation..."):
            result = self.client.chat(
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
        self.messages = [self.messages[0], {
            "role": "user",
            "content": ("[Context was compacted. Summary of the session so far:]\n\n"
                        + summary +
                        "\n\n[Continue helping the user from this state.]"),
        }, {
            "role": "assistant",
            "content": "Understood — I have the session summary and will continue from there.",
        }]
        return f"Compacted to ~{self.context_estimate():,} tokens."
