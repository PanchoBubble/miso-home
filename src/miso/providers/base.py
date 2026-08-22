"""Provider-neutral model boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Iterator, Mapping, Protocol, Sequence, runtime_checkable


class ProviderError(RuntimeError):
    """Bounded provider failure safe to expose to routing logic."""


class ProviderCancelled(ProviderError):
    """Provider request was cancelled by its caller."""


class ProviderProtocolError(ProviderError):
    """Provider returned an invalid or unsupported response."""


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: Sequence[Mapping[str, str]]
    model: str | None = None
    tools: Sequence[Mapping[str, object]] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ChatChunk:
    text: str = ""
    tool_call: Mapping[str, object] | None = None
    done: bool = False
    progress: str | None = None
    provider: str | None = None
    route_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    available: bool
    detail: str
    model: str | None = None


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def health(self) -> ProviderHealth: ...

    def stream(self, request: ChatRequest, cancel: Event) -> Iterator[ChatChunk]: ...
