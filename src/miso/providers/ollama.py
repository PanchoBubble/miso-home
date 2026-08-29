"""Ollama adapter using its local streaming HTTP API."""

from __future__ import annotations

import json
from threading import Event
from typing import Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from miso.providers.base import (
    ChatChunk,
    ChatRequest,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderProtocolError,
)
from miso.providers.tools import ollama_tools


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _partial_tag_length(text: str, tag: str) -> int:
    """Length of a trailing fragment that a later chunk could complete into tag."""
    for size in range(min(len(text), len(tag) - 1), 0, -1):
        if text[-size:].casefold() == tag[:size]:
            return size
    return 0


class _ThinkFilter:
    """Drop reasoning spans from content for models that inline them.

    Ollama releases before 0.9 ignore the `think` request field, so qwen3 still
    emits a full `<think>...</think>` pass in `message.content`. Speaking that
    aloud is wrong, and stripping it after the fact is what leaves the reply
    looking empty. Only a trailing fragment that could still grow into a tag is
    withheld, so live captions stay responsive.
    """

    def __init__(self) -> None:
        self._inside = False
        self._pending = ""

    def feed(self, text: str) -> str:
        self._pending += text
        visible: list[str] = []
        while self._pending:
            if self._inside:
                index = self._pending.casefold().find(_THINK_CLOSE)
                if index < 0:
                    keep = _partial_tag_length(self._pending, _THINK_CLOSE)
                    self._pending = self._pending[len(self._pending) - keep:] if keep else ""
                    break
                self._pending = self._pending[index + len(_THINK_CLOSE):]
                self._inside = False
                continue
            index = self._pending.casefold().find(_THINK_OPEN)
            if index < 0:
                keep = _partial_tag_length(self._pending, _THINK_OPEN)
                cut = len(self._pending) - keep
                visible.append(self._pending[:cut])
                self._pending = self._pending[cut:]
                break
            visible.append(self._pending[:index])
            self._pending = self._pending[index + len(_THINK_OPEN):]
            self._inside = True
        return "".join(visible)

    def flush(self) -> str:
        if self._inside:
            self._pending = ""
            return ""
        remainder, self._pending = self._pending, ""
        return remainder


class OllamaProvider:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 120,
        *,
        provider_name: str = "pi-ollama",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.provider_name = provider_name

    @property
    def name(self) -> str:
        return self.provider_name

    def health(self) -> ProviderHealth:
        try:
            with urlopen(f"{self.base_url}/api/tags", timeout=min(self.timeout, 5)) as response:
                payload = json.load(response)
            names = {item.get("name") for item in payload.get("models", [])}
            installed = self.model in names or f"{self.model}:latest" in names
            detail = "ready" if installed else "model_not_installed"
            return ProviderHealth(installed, detail, self.model)
        except (OSError, ValueError, HTTPError, URLError) as error:
            return ProviderHealth(False, f"unavailable:{type(error).__name__}", self.model)

    def stream(self, request: ChatRequest, cancel: Event) -> Iterator[ChatChunk]:
        if cancel.is_set():
            raise ProviderCancelled("request cancelled before dispatch")
        payload: dict[str, object] = {
            "model": request.model or self.model,
            "messages": list(request.messages),
            "stream": True,
            "think": False,
            "options": {"temperature": 0, "seed": 0},
        }
        if request.tools:
            payload["tools"] = ollama_tools(request.tools)
        emitted = False
        try:
            for chunk in self._dispatch(payload, cancel):
                emitted = True
                yield chunk
        except HTTPError as error:
            # Ollama rejects `think` outright for models it does not classify
            # as thinking-capable. Retry once without it rather than losing the
            # turn; the filter still strips any inline reasoning span. Never
            # retry once output is visible, or the answer would be duplicated.
            if emitted or error.code != 400 or "think" not in payload:
                raise ProviderError("Ollama request failed: HTTPError") from error
            payload.pop("think")
            try:
                yield from self._dispatch(payload, cancel)
            except HTTPError as retry_error:
                raise ProviderError(
                    "Ollama request failed: HTTPError"
                ) from retry_error

    def _dispatch(
        self, payload: dict[str, object], cancel: Event
    ) -> Iterator[ChatChunk]:
        """Stream one /api/chat attempt, letting HTTPError through for retry."""
        http_request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        think_filter = _ThinkFilter()
        try:
            with urlopen(http_request, timeout=self.timeout) as response:
                for raw_line in response:
                    if cancel.is_set():
                        raise ProviderCancelled("request cancelled")
                    if not raw_line.strip():
                        continue
                    data = json.loads(raw_line)
                    message = data.get("message") or {}
                    content = message.get("content") or ""
                    if content:
                        visible = think_filter.feed(content)
                        if visible:
                            yield ChatChunk(text=visible)
                    for call in message.get("tool_calls") or ():
                        function = call.get("function") if isinstance(call, Mapping) else None
                        if not isinstance(function, Mapping):
                            raise ProviderProtocolError("invalid Ollama tool call")
                        name = function.get("name")
                        arguments = function.get("arguments")
                        if not isinstance(name, str) or not isinstance(arguments, Mapping):
                            raise ProviderProtocolError("invalid Ollama tool-call function")
                        yield ChatChunk(tool_call={"name": name, "arguments": dict(arguments)})
                    if data.get("done"):
                        remainder = think_filter.flush()
                        if remainder:
                            yield ChatChunk(text=remainder)
                        yield ChatChunk(done=True)
                        return
        except ProviderError:
            raise
        except HTTPError:
            raise
        except (OSError, ValueError, URLError) as error:
            raise ProviderError(
                f"Ollama request failed: {type(error).__name__}"
            ) from error


class LanOllamaProvider(OllamaProvider):
    """Ollama adapter explicitly identified as a separate LAN escalation tier."""

    def __init__(self, base_url: str, model: str, timeout: float = 120) -> None:
        super().__init__(
            base_url,
            model,
            timeout,
            provider_name="lan-ollama",
        )
