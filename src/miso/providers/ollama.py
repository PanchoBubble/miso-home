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
        }
        if request.tools:
            payload["tools"] = ollama_tools(request.tools)
        http_request = Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
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
                        yield ChatChunk(text=content)
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
                        yield ChatChunk(done=True)
                        return
        except ProviderError:
            raise
        except (OSError, ValueError, HTTPError, URLError) as error:
            raise ProviderError(f"Ollama request failed: {type(error).__name__}") from error


class LanOllamaProvider(OllamaProvider):
    """Ollama adapter explicitly identified as a separate LAN escalation tier."""

    def __init__(self, base_url: str, model: str, timeout: float = 120) -> None:
        super().__init__(
            base_url,
            model,
            timeout,
            provider_name="lan-ollama",
        )
