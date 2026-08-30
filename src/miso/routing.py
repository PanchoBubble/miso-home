"""Deterministic, health-aware provider routing with bounded fallback."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable

from miso.config import Settings
from miso.identity import Actor, VOICE_ACTOR
from miso.providers import (
    ChatChunk,
    ChatRequest,
    GenerationMetrics,
    ModelProvider,
    ProviderCancelled,
    ProviderError,
    ProviderHealth,
    ProviderProtocolError,
    ProviderSet,
    create_provider_set,
)
from miso.tools.audit import AuditSink, JsonlAuditLog, audit_event


class RoutingError(ProviderError):
    """No provider completed the routed request safely."""


class RouteClass(str, Enum):
    AUTO = "auto"
    ROUTINE = "routine"
    STANDARD = "standard"
    COMPLEX = "complex"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route_id: str
    classification: RouteClass
    candidates: tuple[str, ...]
    reason: str
    selected_tools: tuple[str, ...]
    manual_override: str | None = None
    actor_id: str = VOICE_ACTOR.actor_id
    actor_source: str = VOICE_ACTOR.source

    def as_dict(self) -> dict[str, object]:
        return {
            "route_id": self.route_id,
            "classification": self.classification.value,
            "candidates": list(self.candidates),
            "reason": self.reason,
            "selected_tools": list(self.selected_tools),
            "manual_override": self.manual_override,
            "actor": self.actor_id,
            "actor_source": self.actor_source,
        }


_COMPLEX_MARKERS = (
        "analyze",
        "analyse",
        "compare",
        "research",
        "reason",
        "write code",
        "debug",
        "analiza",
        "compara",
        "investiga",
        "razona",
        "código",
        "codigo",
)

# Requests that reach the router always prefer the strongest fast providers:
# deterministic tool intents are answered by the fast lane before routing, so
# the model lane no longer detours through the small on-device model, which is
# kept only as the offline fallback of last resort.
PROVIDER_PREFERENCE = ("hosted-gpt", "codex-cli", "lan-ollama", "pi-ollama")


class ProviderRouter:
    def __init__(
        self,
        providers: ProviderSet,
        audit_sink: AuditSink,
        *,
        health_timeout_seconds: float = 2.0,
        attempt_timeout_seconds: float = 45.0,
        stream_timeout_seconds: float = 300.0,
        health_cache_seconds: float = 20.0,
    ) -> None:
        if not 0 < health_timeout_seconds <= 30:
            raise ValueError("health timeout must be between 0 and 30 seconds")
        if not 0 <= health_cache_seconds <= 300:
            raise ValueError("health cache must be between 0 and 300 seconds")
        if not 0 < attempt_timeout_seconds <= 600:
            raise ValueError("attempt timeout must be between 0 and 600 seconds")
        if not 0 < stream_timeout_seconds <= 3_600:
            raise ValueError("stream timeout must be between 0 and 3600 seconds")
        if stream_timeout_seconds < attempt_timeout_seconds:
            raise ValueError("stream timeout must not be below the attempt timeout")
        self.providers = providers
        self.audit_sink = audit_sink
        self.health_timeout_seconds = health_timeout_seconds
        self.attempt_timeout_seconds = attempt_timeout_seconds
        self.stream_timeout_seconds = stream_timeout_seconds
        self.health_cache_seconds = health_cache_seconds
        self._health_cache: dict[str, tuple[ProviderHealth, float]] = {}
        self._health_cache_lock = threading.Lock()

    def classify(self, request: ChatRequest) -> tuple[RouteClass, str]:
        latest = self._latest_user(request)
        if len(latest) >= 500:
            return RouteClass.COMPLEX, "long user request"
        marker = next((item for item in _COMPLEX_MARKERS if item in latest), None)
        if marker is not None:
            return RouteClass.COMPLEX, f"complexity marker: {marker}"
        return RouteClass.STANDARD, "default hosted-first policy"

    def health_snapshot(self) -> list[dict[str, object]]:
        providers = list(self._providers().values())
        outcomes: queue.Queue[tuple[str, ProviderHealth | Exception, int]] = queue.Queue()

        def check(provider: ModelProvider) -> None:
            started = time.monotonic()
            try:
                result: ProviderHealth | Exception = provider.health()
            except Exception as error:
                result = error
            outcomes.put(
                (
                    provider.name,
                    result,
                    max(0, round((time.monotonic() - started) * 1000)),
                )
            )

        for provider in providers:
            threading.Thread(
                target=check,
                args=(provider,),
                name=f"miso-health-{provider.name}",
                daemon=True,
            ).start()
        deadline = time.monotonic() + self.health_timeout_seconds
        results: dict[str, dict[str, object]] = {}
        while len(results) < len(providers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                name, result, latency_ms = outcomes.get(timeout=remaining)
            except queue.Empty:
                break
            if isinstance(result, ProviderHealth):
                self._store_health(name, result)
                results[name] = {
                    "name": name,
                    "available": result.available,
                    "detail": result.detail,
                    "model": result.model,
                    "latency_ms": latency_ms,
                }
            else:
                results[name] = {
                    "name": name,
                    "available": False,
                    "detail": type(result).__name__,
                    "model": None,
                    "latency_ms": latency_ms,
                }
        return [
            results.get(
                provider.name,
                {
                    "name": provider.name,
                    "available": False,
                    "detail": "health_timeout",
                    "model": None,
                    "latency_ms": round(self.health_timeout_seconds * 1000),
                },
            )
            for provider in providers
        ]

    def plan(
        self,
        request: ChatRequest,
        *,
        route_class: RouteClass | str = RouteClass.AUTO,
        manual_override: str | None = None,
        override_fallback: bool = False,
        actor: Actor = VOICE_ACTOR,
    ) -> RouteDecision:
        try:
            requested_class = RouteClass(route_class)
        except ValueError as error:
            raise RoutingError(f"unknown route class: {route_class}") from error
        if requested_class is RouteClass.AUTO:
            classification, reason = self.classify(request)
        else:
            classification = requested_class
            reason = "explicit route class"
        available = self._providers()
        selected_tools = tuple(
            str(tool["name"])
            for tool in request.tools
            if isinstance(tool.get("name"), str)
        )
        candidates = tuple(
            name for name in PROVIDER_PREFERENCE if name in available
        )
        if manual_override is not None:
            if manual_override not in available:
                raise RoutingError(f"unknown provider override: {manual_override}")
            candidates = (
                (manual_override,)
                if not override_fallback
                else (manual_override, *(name for name in candidates if name != manual_override))
            )
            reason = f"manual provider override: {manual_override}"
        return RouteDecision(
            route_id=str(uuid.uuid4()),
            classification=classification,
            candidates=candidates,
            reason=reason,
            selected_tools=selected_tools,
            manual_override=manual_override,
            actor_id=actor.actor_id,
            actor_source=actor.source,
        )

    def stream(
        self,
        request: ChatRequest,
        cancel: threading.Event,
        *,
        route_class: RouteClass | str = RouteClass.AUTO,
        manual_override: str | None = None,
        override_fallback: bool = False,
        actor: Actor = VOICE_ACTOR,
    ):
        started = time.monotonic()
        decision = self.plan(
            request,
            route_class=route_class,
            manual_override=manual_override,
            override_fallback=override_fallback,
            actor=actor,
        )
        self.audit_sink.record(audit_event("routing_decision", **decision.as_dict()))
        yield self._progress(
            decision,
            f"Routing {decision.classification.value} request ({decision.reason})",
        )
        failures: list[str] = []
        for provider_name in decision.candidates:
            if cancel.is_set():
                self._record_finish(decision, started, "cancelled", None, failures)
                raise ProviderCancelled("routed request was cancelled")
            provider = self._providers()[provider_name]
            yield self._progress(decision, f"Checking {provider_name}", provider_name)
            attempt_started = time.monotonic()
            output_started = False
            try:
                health = self._cached_health(provider, cancel)
                if not health.available:
                    raise ProviderError(f"health:{health.detail}")
                yield self._progress(decision, f"Using {provider_name}", provider_name)
                completed = False
                metrics = None
                for chunk in self._bounded_stream(provider, request, cancel):
                    if chunk.text or chunk.tool_call is not None:
                        output_started = True
                    if chunk.done:
                        completed = True
                        metrics = chunk.metrics
                    yield replace(
                        chunk,
                        provider=provider_name,
                        route_id=decision.route_id,
                    )
                if not completed:
                    raise ProviderProtocolError("provider stream ended without completion")
                self._record_attempt(
                    decision,
                    provider_name,
                    attempt_started,
                    "completed",
                    None,
                    metrics,
                )
                self._record_finish(
                    decision,
                    started,
                    "completed",
                    provider_name,
                    failures,
                )
                return
            except ProviderCancelled:
                self._record_attempt(
                    decision, provider_name, attempt_started, "cancelled", "cancelled"
                )
                self._record_finish(
                    decision, started, "cancelled", provider_name, failures
                )
                raise
            except Exception as error:
                self._forget_health(provider_name)
                reason = self._bounded_error(error)
                failures.append(f"{provider_name}:{reason}")
                self._record_attempt(
                    decision, provider_name, attempt_started, "failed", reason
                )
                if output_started:
                    self._record_finish(
                        decision, started, "failed", provider_name, failures
                    )
                    raise RoutingError(
                        f"{provider_name} failed after streaming began: {reason}"
                    ) from error
                yield self._progress(
                    decision,
                    f"{provider_name} unavailable; trying fallback",
                    provider_name,
                )
        self._record_finish(decision, started, "failed", None, failures)
        raise RoutingError("all routed providers failed: " + "; ".join(failures))

    def _providers(self) -> dict[str, ModelProvider]:
        return {
            provider.name: provider
            for provider in self.providers.configured()
        }

    def _cached_health(
        self, provider: ModelProvider, cancel: threading.Event
    ) -> ProviderHealth:
        """Reuse a recent health verdict so a turn starts streaming immediately.

        Only a positive verdict is cached: an unavailable provider must be
        re-checked next turn so recovery is noticed within one request, and a
        mid-stream failure evicts the entry via _forget_health.
        """
        if self.health_cache_seconds > 0:
            with self._health_cache_lock:
                cached = self._health_cache.get(provider.name)
            if cached is not None and time.monotonic() - cached[1] < self.health_cache_seconds:
                return cached[0]
        health = self._bounded_call(
            provider.health,
            self.health_timeout_seconds,
            cancel,
            "provider health check",
        )
        if not isinstance(health, ProviderHealth):
            raise ProviderProtocolError("provider returned invalid health")
        self._store_health(provider.name, health)
        return health

    def _store_health(self, name: str, health: ProviderHealth) -> None:
        if self.health_cache_seconds <= 0 or not health.available:
            return
        with self._health_cache_lock:
            self._health_cache[name] = (health, time.monotonic())

    def _forget_health(self, name: str) -> None:
        with self._health_cache_lock:
            self._health_cache.pop(name, None)

    @staticmethod
    def _latest_user(request: ChatRequest) -> str:
        return next(
            (
                message.get("content", "")
                for message in reversed(request.messages)
                if message.get("role") == "user"
            ),
            "",
        ).casefold()

    def _bounded_stream(
        self,
        provider: ModelProvider,
        request: ChatRequest,
        caller_cancel: threading.Event,
    ):
        events: queue.Queue[tuple[str, object]] = queue.Queue()
        attempt_cancel = threading.Event()

        def run() -> None:
            try:
                for chunk in provider.stream(request, attempt_cancel):
                    events.put(("chunk", chunk))
                events.put(("end", None))
            except BaseException as error:
                events.put(("error", error))

        threading.Thread(
            target=run,
            name=f"miso-provider-{provider.name}",
            daemon=True,
        ).start()
        # The attempt timeout bounds silence between chunks, not the whole answer:
        # a small local model can stream for minutes and must not be killed while
        # it is still producing tokens. stream_timeout_seconds is the hard ceiling.
        ceiling = time.monotonic() + self.stream_timeout_seconds
        deadline = time.monotonic() + self.attempt_timeout_seconds
        while True:
            if caller_cancel.is_set():
                attempt_cancel.set()
                raise ProviderCancelled("routed request was cancelled")
            now = time.monotonic()
            if ceiling - now <= 0:
                attempt_cancel.set()
                raise ProviderError("provider stream exceeded its total budget")
            remaining = min(deadline, ceiling) - now
            if remaining <= 0:
                attempt_cancel.set()
                raise ProviderError("provider attempt timed out")
            try:
                kind, value = events.get(timeout=min(remaining, 0.025))
            except queue.Empty:
                continue
            if kind == "chunk":
                if not isinstance(value, ChatChunk):
                    raise ProviderProtocolError("provider yielded invalid chunk")
                deadline = time.monotonic() + self.attempt_timeout_seconds
                yield value
            elif kind == "error":
                if isinstance(value, BaseException):
                    raise value
                raise ProviderError("provider failed")
            else:
                return

    @staticmethod
    def _bounded_call(
        function: Callable[[], object],
        timeout: float,
        cancel: threading.Event,
        label: str,
    ) -> object:
        outcomes: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                outcomes.put_nowait((True, function()))
            except BaseException as error:
                outcomes.put_nowait((False, error))

        threading.Thread(target=run, name="miso-provider-health", daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            if cancel.is_set():
                raise ProviderCancelled("routed request was cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(f"{label} timed out")
            try:
                succeeded, value = outcomes.get(timeout=min(remaining, 0.025))
            except queue.Empty:
                continue
            if succeeded:
                return value
            if isinstance(value, BaseException):
                raise value
            raise ProviderError(f"{label} failed")

    @staticmethod
    def _progress(
        decision: RouteDecision,
        message: str,
        provider: str | None = None,
    ) -> ChatChunk:
        return ChatChunk(
            progress=message,
            provider=provider,
            route_id=decision.route_id,
        )

    @staticmethod
    def _bounded_error(error: Exception) -> str:
        if isinstance(error, ProviderError):
            return str(error)[:240]
        return type(error).__name__

    def _record_attempt(
        self,
        decision: RouteDecision,
        provider: str,
        started: float,
        status: str,
        reason: str | None,
        metrics: GenerationMetrics | None = None,
    ) -> None:
        self.audit_sink.record(
            audit_event(
                "provider_attempt_finished",
                route_id=decision.route_id,
                provider=provider,
                status=status,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                reason=reason,
                generation=None if metrics is None else metrics.as_dict(),
                actor=decision.actor_id,
                actor_source=decision.actor_source,
            )
        )

    def _record_finish(
        self,
        decision: RouteDecision,
        started: float,
        status: str,
        provider: str | None,
        failures: list[str],
    ) -> None:
        self.audit_sink.record(
            audit_event(
                "routing_finished",
                route_id=decision.route_id,
                classification=decision.classification.value,
                status=status,
                selected_provider=provider,
                latency_ms=max(0, round((time.monotonic() - started) * 1000)),
                failures=list(failures),
                actor=decision.actor_id,
                actor_source=decision.actor_source,
            )
        )


def create_router(settings: Settings) -> ProviderRouter:
    return ProviderRouter(
        create_provider_set(settings),
        JsonlAuditLog(settings.state_dir / "audit" / "routing.jsonl"),
        health_timeout_seconds=settings.routing_health_timeout_seconds,
        attempt_timeout_seconds=settings.routing_attempt_timeout_seconds,
        stream_timeout_seconds=settings.routing_stream_timeout_seconds,
        health_cache_seconds=settings.routing_health_cache_seconds,
    )
