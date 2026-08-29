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
class GenerationMetrics:
    """Where a turn's latency actually went, as reported by the provider.

    Prompt evaluation and token generation fail for different reasons and are
    fixed differently: a slow prompt means too much replayed history, a slow
    generation means the model is too large. Without the split, tuning either
    one is guesswork.
    """

    prompt_tokens: int
    prompt_milliseconds: int
    generated_tokens: int
    generation_milliseconds: int

    @property
    def total_milliseconds(self) -> int:
        return self.prompt_milliseconds + self.generation_milliseconds

    @property
    def tokens_per_second(self) -> float:
        if self.generation_milliseconds <= 0:
            return 0.0
        return round(self.generated_tokens / (self.generation_milliseconds / 1000), 2)

    def as_dict(self) -> dict[str, object]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "prompt_ms": self.prompt_milliseconds,
            "generated_tokens": self.generated_tokens,
            "generation_ms": self.generation_milliseconds,
            "tokens_per_second": self.tokens_per_second,
        }


@dataclass(frozen=True, slots=True)
class ChatChunk:
    text: str = ""
    tool_call: Mapping[str, object] | None = None
    done: bool = False
    progress: str | None = None
    provider: str | None = None
    route_id: str | None = None
    metrics: GenerationMetrics | None = None


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
