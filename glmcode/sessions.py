"""Persistent chat sessions: each session stores its conversation, work folder
and token usage in ~/.makenomistakes/sessions/<id>.json, like Claude Code / Codex."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .prompts import (CONTINUE_NUDGE, EXECUTE_PLAN_MESSAGE, FILE_CONTEXT_MARKER,
                      FRESH_REVIEW_HEADER, PLAN_MODE_PREAMBLE, REFINE_NUDGE,
                      STEER_NUDGE_TEMPLATE, STEP_LIMIT_NUDGE, VERIFY_NUDGE,
                      WRAP_UP_NUDGE)

# Internal plumbing messages the agent injects mid-turn; they were never
# typed by the user, so history replay must not render them as user bubbles.
_INTERNAL_NUDGES = {CONTINUE_NUDGE, STEP_LIMIT_NUDGE, VERIFY_NUDGE,
                    REFINE_NUDGE, WRAP_UP_NUDGE}
# Some of those carry variable content (a detected command, the reviewer's
# findings, test output), so exact-match isn't enough -- match their stable
# leading text too.
_INTERNAL_NUDGE_PREFIXES = (VERIFY_NUDGE, REFINE_NUDGE, FRESH_REVIEW_HEADER,
                            "[Automatic test run -- not from the user]")
# STEER_NUDGE_TEMPLATE-wrapped messages ARE from the user -- shown as the
# same "You steered" note the live view used, not as a framed wall of text.
_STEER_PREFIX = STEER_NUDGE_TEMPLATE.split("{text}")[0]
# Same for plan-mode wrapping: replay shows the user's own words + a badge.
_PLAN_PREFIX = PLAN_MODE_PREAMBLE.split("{text}")[0]

_ASSET_MARKER_RE = re.compile(r"^\[(image|audio): (.*?)\](?:\s*\[caption: (.*?)\])?\s*")


def _extract_asset_marker(text: str) -> tuple[str, str, str, str]:
    """Pull the kind/path/caption out of a generate_image/show_image/speak
    tool result (see agent.Agent._asset_marker) so history replay can
    rebuild the inline image/audio card instead of showing the raw marker
    as tool-result text. Returns (kind, path, caption, rest)."""
    m = _ASSET_MARKER_RE.match(text or "")
    if not m:
        return "", "", "", text or ""
    kind, path, caption = m.group(1), m.group(2), m.group(3) or ""
    return kind, path, caption, text[m.end():].strip()

SESSIONS_DIR = CONFIG_DIR / "sessions"


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_title(messages: list) -> str:
    """First real user message, cleaned up, as the session title."""
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        if not isinstance(c, str):
            continue
        text = c.split("\n\n[Image analysis:")[0].strip()
        if text.startswith("[Context was compacted"):
            continue
        text = " ".join(text.split())
        if text:
            return text[:64] + ("…" if len(text) > 64 else "")
    return "New chat"


class SessionStore:
    def __init__(self, root: Path = SESSIONS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, sid: str) -> Path:
        safe = "".join(ch for ch in sid if ch.isalnum() or ch in "-_")
        return self.root / f"{safe}.json"

    def save(self, sid: str, cwd: str, messages: list,
             prompt_tokens: int, completion_tokens: int,
             todos: list | None = None, title: str = "",
             auto_backup: bool = True,
             model_provider: str = "", model: str = "",
             turn_snapshots: list | None = None) -> None:
        body = [m for m in messages if m.get("role") != "system"]
        if not body:
            return  # never persist a session with no messages yet
        path = self._path(sid)
        created = _now()
        if path.exists():
            try:
                created = json.loads(path.read_text(encoding="utf-8")).get("created", created)
            except (json.JSONDecodeError, OSError):
                pass
        data = {
            "id": sid,
            "title": title or derive_title(body),
            "cwd": cwd,
            "created": created,
            "updated": _now(),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "todos": todos or [],
            "messages": body,
            "auto_backup": auto_backup,
            # Per-chat model choice ("" = the built-in free default).
            "model_provider": model_provider,
            "model": model,
            # One pre-turn shadow-git commit per send-turn, in order; powers
            # "edit & resend" (revert files to that turn, then rewind).
            "turn_snapshots": turn_snapshots or [],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def load(self, sid: str) -> dict | None:
        path = self._path(sid)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def delete(self, sid: str) -> None:
        try:
            self._path(sid).unlink(missing_ok=True)
        except OSError:
            pass

    def list(self) -> list[dict]:
        out = []
        for f in self.root.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({
                    "id": data.get("id", f.stem),
                    "title": data.get("title", "Untitled"),
                    "cwd": data.get("cwd", ""),
                    "updated": data.get("updated", ""),
                    "messages": len(data.get("messages", [])),
                })
            except (json.JSONDecodeError, OSError):
                continue
        out.sort(key=lambda s: s["updated"], reverse=True)
        return out


# --------------------------------------------------------------------- #
# Transcript -> display items (for re-rendering a loaded session in the app)

def _compacted_summary(text: str) -> str:
    """Pull the retained summary out of a compaction marker message so the UI
    can show what the conversation was compacted down to."""
    body = text.split("]", 1)[1] if "]" in text else text
    tail = "\n\n[Continue helping"
    if tail in body:
        body = body.split(tail, 1)[0]
    return body.strip()


def to_display(messages: list) -> list[dict]:
    items: list[dict] = []
    results = {m.get("tool_call_id"): m.get("content", "")
               for m in messages if m.get("role") == "tool"}
    # Per real user turn, in order, so "edit & resend" can name which turn to
    # rewind to (turn_ordinal) and where to truncate (msg_index, absolute in
    # `messages`). Only genuine user turns count -- steering/nudge/compaction
    # user-role messages don't (they `continue` before the user append).
    turn_ordinal = 0

    for abs_idx, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            c = m.get("content")
            images: list[str] = []
            described = False
            if isinstance(c, list):
                text = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
                images = [p["image_url"]["url"] for p in c if p.get("type") == "image_url"]
            else:
                text = c or ""
            # Auto-attached @-file contents are for the model only -- the user
            # sees just their own text and the @mentions they typed.
            if FILE_CONTEXT_MARKER in text:
                text = text.split(FILE_CONTEXT_MARKER, 1)[0]
            if text.startswith("[Context was compacted"):
                items.append({"kind": "compacted", "summary": _compacted_summary(text)})
                continue
            if text in _INTERNAL_NUDGES or text.startswith(_INTERNAL_NUDGE_PREFIXES):
                # Internal agent plumbing (auto-continue, step-limit/wrap-up,
                # verify and review nudges); not real user messages, don't render.
                continue
            if text.startswith(_STEER_PREFIX):
                items.append({"kind": "steered",
                              "text": text[len(_STEER_PREFIX):].strip()})
                continue
            plan = False
            if text.startswith(_PLAN_PREFIX):
                text = text[len(_PLAN_PREFIX):]
                plan = True
            elif text == EXECUTE_PLAN_MESSAGE:
                text = "Execute the approved plan."
            marker = "\n\n[Image analysis:"
            if marker in text:
                text = text.split(marker, 1)[0]
                described = True
            items.append({"kind": "user", "text": text.strip(),
                         "images": images, "described": described, "plan": plan,
                         "msg_index": abs_idx, "turn_ordinal": turn_ordinal})
            turn_ordinal += 1
        elif role == "assistant":
            content = m.get("content")
            if content and content != ("Understood — I have the session summary "
                                       "and will continue from there."):
                items.append({"kind": "assistant", "text": content})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                res = results.get(tc.get("id"), "")
                name = fn.get("name", "?")
                is_error = res.startswith("ERROR") or res.startswith("User denied")
                if name in ("generate_image", "show_image", "speak") and not is_error:
                    kind, path, caption, clean = _extract_asset_marker(res)
                    item_kind = "tool_audio" if kind == "audio" else "tool_image"
                    items.append({"kind": item_kind, "name": name, "path": path,
                                 "caption": caption, "result": clean})
                    continue
                items.append({
                    "kind": "tool",
                    "name": name,
                    "args": args,
                    "result": res[:12000],
                    "error": is_error,
                })
    return items
