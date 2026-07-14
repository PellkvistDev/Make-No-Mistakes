"""Persistent chat sessions: each session stores its conversation, work folder
and token usage in ~/.glmcode/sessions/<id>.json, like Claude Code / Codex."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .prompts import CONTINUE_NUDGE

_IMAGE_MARKER_RE = re.compile(r"^\[image: (.*?)\](?:\s*\[caption: (.*?)\])?\s*")


def _extract_image_marker(text: str) -> tuple[str, str, str]:
    """Pull the path/caption out of a generate_image/show_image tool result
    (see agent.Agent._image_marker) so history replay can rebuild the inline
    image card instead of showing the raw marker as tool-result text."""
    m = _IMAGE_MARKER_RE.match(text or "")
    if not m:
        return "", "", text or ""
    path, caption = m.group(1), m.group(2) or ""
    return path, caption, text[m.end():].strip()

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
             todos: list | None = None, title: str = "") -> None:
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
    body = [m for m in messages if m.get("role") != "system"]
    results = {m.get("tool_call_id"): m.get("content", "")
               for m in body if m.get("role") == "tool"}

    for m in body:
        role = m.get("role")
        if role == "user":
            c = m.get("content")
            images: list[str] = []
            described = False
            if isinstance(c, list):
                text = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
                images = [p["image_url"]["url"] for p in c if p.get("type") == "image_url"]
            else:
                text = c or ""
            if text.startswith("[Context was compacted"):
                items.append({"kind": "compacted", "summary": _compacted_summary(text)})
                continue
            if text == CONTINUE_NUDGE:
                # Internal nudge from the auto-continue-on-truncation logic
                # (agent.py); not a real user message, so don't render it.
                continue
            marker = "\n\n[Image analysis:"
            if marker in text:
                text = text.split(marker, 1)[0]
                described = True
            items.append({"kind": "user", "text": text.strip(),
                         "images": images, "described": described})
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
                if name in ("generate_image", "show_image") and not is_error:
                    path, caption, clean = _extract_image_marker(res)
                    items.append({"kind": "tool_image", "name": name, "path": path,
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
