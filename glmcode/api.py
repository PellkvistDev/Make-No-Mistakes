"""z.ai (Zhipu) API client: OpenAI-compatible chat completions with SSE streaming.

Handles:
- streaming deltas (content, reasoning_content, tool_calls) with index-based merging
- automatic retry with backoff on 429/5xx (the free tier is rate-limited to ~1 req/s)
- vision requests (image_url content parts with base64 data URIs)
"""

from __future__ import annotations

import base64
import json
import mimetypes
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"API error {status}: {message}")
        self.status = status
        self.message = message


class Cancelled(Exception):
    """Raised when the user cancels a streaming request."""


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list = field(default_factory=list)
    finish_reason: str = ""
    usage: Usage = field(default_factory=Usage)

    def to_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRIES = 6


class RateLimiter:
    """Spaces out calls across threads so parallel sub-agents don't all hit
    the free tier's ~1 req/s limit at the same moment -- which just burns
    their own retry budget and tool-calling step budget on 429 backoff
    instead of real task progress. Share one instance across every
    ZaiClient spawned for a single spawn_agents call; unused (no-op) for
    the single-threaded main agent, which never contends with itself."""

    def __init__(self, min_interval: float = 1.05):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class ZaiClient:
    def __init__(self, api_key: str, base_url: str, rate_limiter: Optional[RateLimiter] = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.rate_limiter = rate_limiter
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })

    # ------------------------------------------------------------------ #

    def chat(
        self,
        model: str,
        messages: list,
        tools: Optional[list] = None,
        temperature: float = 0.6,
        max_tokens: int = 8192,
        thinking: bool = True,
        on_content: Optional[Callable[[str], None]] = None,
        on_reasoning: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        cancel=None,
    ) -> ChatResult:
        """Send a chat completion request, streaming. Returns the final result."""
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if thinking:
            payload["thinking"] = {"type": "enabled"}

        last_err: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            if attempt > 0:
                wait = min(2 ** attempt, 30)
                if isinstance(last_err, ApiError) and last_err.status == 429:
                    wait = max(wait, 2)
                if on_status:
                    on_status(f"retrying in {wait}s ({last_err})")
                time.sleep(wait)
            if self.rate_limiter:
                self.rate_limiter.wait()
            try:
                return self._stream_once(payload, on_content, on_reasoning, cancel)
            except ApiError as e:
                if e.status in RETRYABLE:
                    last_err = e
                    continue
                raise
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                continue
        raise ApiError(0, f"gave up after {MAX_RETRIES} attempts: {last_err}")

    # ------------------------------------------------------------------ #

    def _stream_once(self, payload, on_content, on_reasoning, cancel=None) -> ChatResult:
        url = f"{self.base_url}/chat/completions"
        result = ChatResult()
        tool_calls: dict[int, dict] = {}

        with self.session.post(url, json=payload, stream=True, timeout=(15, 300)) as resp:
            if resp.status_code != 200:
                try:
                    body = resp.json()
                    msg = body.get("error", {}).get("message") or json.dumps(body)[:500]
                except Exception:
                    msg = resp.text[:500]
                raise ApiError(resp.status_code, msg)

            for raw in resp.iter_lines(decode_unicode=True):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if not raw or not raw.startswith("data:"):
                    continue
                data = raw[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if chunk.get("usage"):
                    u = chunk["usage"]
                    result.usage = Usage(
                        u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
                    )
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    result.finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}

                reasoning = delta.get("reasoning_content")
                if reasoning:
                    result.reasoning += reasoning
                    if on_reasoning:
                        on_reasoning(reasoning)

                content = delta.get("content")
                if content:
                    result.content += content
                    if on_content:
                        on_content(content)

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(idx, {
                        "id": "", "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    if tc.get("type"):
                        slot["type"] = tc["type"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

        result.tool_calls = [tool_calls[i] for i in sorted(tool_calls)]
        for i, tc in enumerate(result.tool_calls):
            if not tc["id"]:
                tc["id"] = f"call_{int(time.time() * 1000)}_{i}"
        return result

    # ------------------------------------------------------------------ #
    # Vision

    def analyze_images(
        self,
        vision_model: str,
        prompt: str,
        image_paths: list[Path],
        max_tokens: int = 4096,
        on_content: Optional[Callable[[str], None]] = None,
    ) -> str:
        """Ask the vision model about local image files. Returns its text answer."""
        content: list = []
        for p in image_paths:
            content.append({
                "type": "image_url",
                "image_url": {"url": encode_image_data_uri(p)},
            })
        content.append({"type": "text", "text": prompt})
        result = self.chat(
            model=vision_model,
            messages=[{"role": "user", "content": content}],
            temperature=0.3,
            max_tokens=max_tokens,
            thinking=False,
            on_content=on_content,
        )
        return result.content.strip()


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def encode_image_data_uri(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"{path.name} is {len(data) // 1024 // 1024}MB; images must be under 8MB"
        )
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def estimate_tokens(messages: list) -> int:
    """Rough token estimate (chars/3.6, safe-side) for context management."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    chars += len(part.get("text", ""))
                else:
                    chars += 4000  # images cost roughly ~1k tokens
        for tc in m.get("tool_calls") or []:
            chars += len(json.dumps(tc))
    return int(chars / 3.6)
