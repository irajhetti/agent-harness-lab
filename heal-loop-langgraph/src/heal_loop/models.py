"""The model interface, its two implementations, and the validate-retry-once helper."""

import json
import re
import urllib.request
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from .hooks import Hooks


class Model(Protocol):
    def complete(self, role: str, prompt: str) -> str:
        """Return the model's reply. `role` is "fixer" or "reviewer"."""
        ...


class ScriptedModel:
    """Deterministic: replays fixed replies per role, in order. Used by every test and the CLI demo."""

    def __init__(self, replies: dict[str, list[str]]) -> None:
        self.replies = {role: list(texts) for role, texts in replies.items()}
        self.calls: list[tuple[str, str]] = []  # (role, prompt), for tests to inspect

    def complete(self, role: str, prompt: str) -> str:
        self.calls.append((role, prompt))
        if not self.replies.get(role):
            raise RuntimeError(f"ScriptedModel has no reply left for {role}")
        return self.replies[role].pop(0)


class OllamaModel:
    """Plain HTTP to a local Ollama-compatible /api/chat endpoint. No key, no other provider."""

    def __init__(self, base_url: str, model: str, timeout_s: int = 300) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("OLLAMA_BASE_URL must start with http:// or https://")
        self.url = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.timeout_s = timeout_s
        self.last_usage: tuple[int, int] | None = None  # (prompt tokens, reply tokens) of the last call

    def complete(self, role: str, prompt: str) -> str:
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": f"You are the {role}. Reply with one JSON object only."},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
            data = json.load(response)
        counts = (data.get("prompt_eval_count"), data.get("eval_count"))  # absent unless the server reports them
        self.last_usage = (counts[0], counts[1]) if all(isinstance(count, int) for count in counts) else None
        return str(data["message"]["content"])


def last_json(text: str) -> Any:
    """The last ```json block``` (or bare object) in a reply, or None. Ports lastJson in heal.ts.

    One addition: a reply that is nothing but JSON is also accepted, because a proposal has
    nested objects and "from the last brace" would only find the innermost one.
    """
    blocks = re.findall(r"```json\s*(.*?)```", text, re.S)
    candidates = blocks or [text, text[text.rfind("{") :]]
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def ask[T: BaseModel](model: Model, role: str, prompt: str, schema: type[T], hooks: Hooks) -> tuple[T | None, str, int]:
    """Ask the model for a `schema`. Returns (parsed or None, last validation error, calls made).

    An invalid reply is retried once with the validation error fed back. A second invalid reply
    returns None, and the caller escalates. A reply is never repaired or guessed at.
    """
    error = ""
    for attempt in (1, 2):
        full = prompt if attempt == 1 else f"{prompt}\n\nYour last reply was not valid:\n{error}\nReply again."
        hooks.on_model_start(role, full)
        try:
            reply = model.complete(role, full)
        except BaseException as failure:
            hooks.on_error(failure)
            raise
        hooks.on_model_call(role, full, reply)
        try:
            return schema.model_validate(last_json(reply)), "", attempt
        except ValidationError as exc:
            error = str(exc)
    return None, error, 2
