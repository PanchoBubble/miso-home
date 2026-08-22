"""Hosted GPT adapter using the OpenAI Responses streaming API."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from miso.providers.base import (
    ChatChunk,
    ChatRequest,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderProtocolError,
)
from miso.providers.tools import openai_tools


class OpenAIResponsesProvider:
    """Dependency-free OpenAI adapter; credentials are never included in payloads."""

    def __init__(
        self,
        api_key: str | None,
        model: str,
        timeout: float = 120,
        *,
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self.model = model
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return "hosted-gpt"

    @property
    def configured(self) -> bool:
        return self._api_key is not None

    def health(self) -> ProviderHealth:
        if self._api_key is None:
            return ProviderHealth(False, "not_configured", self.model)
        request = Request(
            f"{self.base_url}/models/{quote(self.model, safe='')}",
            headers=self._headers(),
            method="GET",
        )
        try:
            with urlopen(request, timeout=min(self.timeout, 5)) as response:
                payload = json.load(response)
            available = payload.get("id") == self.model
            return ProviderHealth(
                available,
                "ready" if available else "model_mismatch",
                self.model,
            )
        except (OSError, ValueError, HTTPError, URLError) as error:
            return ProviderHealth(
                False,
                f"unavailable:{type(error).__name__}",
                self.model,
            )

    def stream(self, request: ChatRequest, cancel: Event) -> Iterator[ChatChunk]:
        if self._api_key is None:
            raise ProviderError("hosted GPT provider is not configured")
        if cancel.is_set():
            raise ProviderCancelled("request cancelled before dispatch")
        payload: dict[str, object] = {
            "model": request.model or self.model,
            "input": list(request.messages),
            "stream": True,
            "store": False,
        }
        if request.tools:
            payload["tools"] = openai_tools(request.tools)
        http_request = Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urlopen(http_request, timeout=self.timeout) as response:
                for data in self._sse_data(response, cancel):
                    event = json.loads(data)
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ProviderProtocolError("invalid GPT text delta")
                        if delta:
                            yield ChatChunk(text=delta)
                    elif event_type == "response.function_call_arguments.done":
                        yield ChatChunk(tool_call=self._tool_call(event))
                    elif event_type == "response.completed":
                        yield ChatChunk(done=True)
                        return
                    elif event_type in {"error", "response.failed", "response.incomplete"}:
                        raise ProviderError(f"GPT response ended with {event_type}")
            raise ProviderProtocolError("GPT stream ended without response.completed")
        except ProviderError:
            raise
        except (OSError, ValueError, HTTPError, URLError) as error:
            raise ProviderError(f"GPT request failed: {type(error).__name__}") from error

    def _headers(self) -> dict[str, str]:
        assert self._api_key is not None
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    @staticmethod
    def _sse_data(response: object, cancel: Event) -> Iterator[str]:
        data_lines: list[str] = []
        for raw_line in response:
            if cancel.is_set():
                raise ProviderCancelled("request cancelled")
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line:
                if data_lines:
                    data = "\n".join(data_lines)
                    data_lines.clear()
                    if data != "[DONE]":
                        yield data
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            data = "\n".join(data_lines)
            if data != "[DONE]":
                yield data

    @staticmethod
    def _tool_call(event: Mapping[str, object]) -> dict[str, object]:
        name = event.get("name")
        arguments = event.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise ProviderProtocolError("invalid GPT function-call event")
        decoded = json.loads(arguments)
        if not isinstance(decoded, Mapping):
            raise ProviderProtocolError("GPT function arguments must be an object")
        return {"name": name, "arguments": dict(decoded)}
