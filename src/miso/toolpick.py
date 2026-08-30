"""Constrained JSON tool selection for requests the fast lane cannot parse.

The picker lane sits between the deterministic fast lane and the model lane.
A request that misses every fast-lane regex but still looks tool-shaped is put
to the small on-device model as a selection problem, not a conversation: the
reply is pinned to JSON and capped at a few dozen tokens, so the round-trip
costs a couple of seconds instead of a full generated answer.

The model only ever selects. Everything after the pick is deterministic: the
name must be one of the allowlisted pickable tools, the arguments must satisfy
that tool's schema before anything runs, and the spoken reply comes from the
same templated renderers the fast lane uses. Output that is malformed,
unexpected, or merely unrecognised never reaches a handler; the request falls
through to the next lane unchanged.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Protocol

from miso.identity import Actor, VOICE_ACTOR
from miso.intake import ReplyRenderer, default_intents
from miso.tools import ToolRegistry, ToolResult, ToolStatus
from miso.tools.audit import AuditSink, InMemoryAuditLog, audit_event
from miso.tools.schema import SchemaError, validate_instance


class JsonCompletion(Protocol):
    """A provider able to answer once, in JSON, under a hard token cap."""

    def complete_json(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = ...,
        timeout_seconds: float | None = ...,
        cancel: threading.Event | None = ...,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PickedReply:
    """A completed pick, shaped like a fast-lane reply so lanes interchange."""

    intent: str
    tool: str
    result: ToolResult
    spoken: str
    duration_ms: int

    def as_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "tool": self.tool,
            "result": self.result.as_dict(),
            "spoken": self.spoken,
            "duration_ms": self.duration_ms,
        }


# Only tools with a templated renderer can be picked. A pick has no second
# model call to phrase its result, so a tool Miso cannot already speak about
# has nothing to say afterwards, and keeping the list explicit means a newly
# registered tool never becomes reachable by model output on its own.
def default_pickable() -> dict[str, ReplyRenderer]:
    return {intent.tool: intent.render for intent in default_intents()}


# Vocabulary that makes a missed utterance worth one selection call. Narrow on
# purpose: a request outside these domains cannot be served by a pickable tool,
# so paying for the round-trip would only delay the lane that can answer it.
_TOOL_SHAPED_WORDS = (
    "timer", "countdown", "alarm", "temporizador", "alarma",
    "cuenta atras", "cuenta atrás", "cronometro", "cronómetro",
    "shopping", "groceries", "grocery", "list", "compra", "compras",
    "lista", "supermercado",
    "weather", "forecast", "rain", "raining", "temperature", "sunny",
    "tiempo", "clima", "llover", "lluvia", "temperatura",
    "pronostico", "pronóstico",
)

# Requests that want reasoning rather than an action belong to the strong
# providers, even when they mention a tool domain in passing.
_REASONING_WORDS = (
    "analyze", "analyse", "compare", "explain", "research", "summarize",
    "summarise", "why", "should i", "recommend", "suggest",
    "analiza", "compara", "explica", "investiga", "resume", "por qué",
    "por que", "recomienda", "sugiere",
)

_MINIMUM_LENGTH = 3
_MAXIMUM_LENGTH = 200
_MAXIMUM_OUTPUT_CHARACTERS = 2_000
_PUNCTUATION = re.compile(r"[¿¡?!.,;:]+")

_SYSTEM_PROMPT = (
    "You select one household tool for the user request.\n"
    "Answer with JSON only, no prose: "
    '{"tool":"<name>","arguments":{...}}.\n'
    'Answer {"tool":null} when no listed tool fits.\n'
    "Tools:\n"
)


def _normalize(text: str) -> str:
    return " ".join(_PUNCTUATION.sub(" ", text.casefold()).split())


def _signature(schema: Mapping[str, object]) -> str:
    properties = schema.get("properties")
    required = schema.get("required")
    names = required if isinstance(required, (list, tuple)) else ()
    if not isinstance(properties, Mapping):
        return ""
    parts = []
    for name, child in properties.items():
        kind = child.get("type", "string") if isinstance(child, Mapping) else "string"
        parts.append(f"{name}:{kind}" if name in names else f"{name}?:{kind}")
    return ", ".join(parts)


def _decode_pick(raw: str) -> tuple[str | None, dict[str, object], str]:
    """Decode one model reply into a candidate name and arguments.

    Returns a name of None whenever the output cannot be read as a selection,
    with a bounded reason suitable for the audit trail.
    """
    text = raw.strip()
    if not text:
        return None, {}, "empty_output"
    if len(text) > _MAXIMUM_OUTPUT_CHARACTERS:
        return None, {}, "output_too_long"
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        return None, {}, "invalid_json"
    if not isinstance(decoded, Mapping):
        return None, {}, "output_is_not_an_object"
    name = decoded.get("tool", decoded.get("name"))
    if name is None:
        return None, {}, "no_tool_selected"
    if not isinstance(name, str):
        return None, {}, "tool_name_is_not_a_string"
    arguments = decoded.get("arguments", decoded.get("parameters", {}))
    if not isinstance(arguments, Mapping):
        return None, {}, "arguments_are_not_an_object"
    if any(not isinstance(key, str) for key in arguments):
        return None, {}, "argument_names_are_not_strings"
    return name, dict(arguments), "decoded"


class ToolPicker:
    """Ask the local model which allowlisted tool a missed request wants.

    Ownership mirrors the fast lane: once a validated pick executes, this lane
    owns the turn including failures, so a mutating tool can never run twice
    for one utterance. A pick the registry rejects before the handler runs is
    the single exception, since nothing happened.
    """

    def __init__(
        self,
        tools: ToolRegistry,
        completion: JsonCompletion | None,
        audit_sink: AuditSink | None = None,
        pickable: Mapping[str, ReplyRenderer] | None = None,
        *,
        enabled: bool = True,
        max_tokens: int = 40,
        timeout_seconds: float = 6.0,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("tool picker token cap must be positive")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("tool picker timeout must be between 0 and 60 seconds")
        self.tools = tools
        self.completion = completion
        self.audit_sink = audit_sink or InMemoryAuditLog()
        self.pickable = dict(default_pickable() if pickable is None else pickable)
        self.enabled = enabled
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    def try_handle(
        self,
        text: str,
        language: str,
        *,
        cancel_event: threading.Event | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> PickedReply | None:
        if not self.enabled or self.completion is None:
            return None
        if cancel_event is not None and cancel_event.is_set():
            return None
        normalized = _normalize(text)
        candidates = self._candidates()
        if not candidates or not self._looks_tool_shaped(normalized):
            return None
        started = time.monotonic()
        try:
            raw = self.completion.complete_json(
                _SYSTEM_PROMPT + self._catalogue(candidates),
                normalized,
                max_tokens=self.max_tokens,
                timeout_seconds=self.timeout_seconds,
                cancel=cancel_event,
            )
        except Exception as error:  # A missed pick must never fail the turn.
            self._record("fell_through", None, type(error).__name__, started, actor)
            return None
        if cancel_event is not None and cancel_event.is_set():
            self._record("fell_through", None, "cancelled", started, actor)
            return None
        name, arguments, reason = _decode_pick(raw)
        if name is None:
            self._record("fell_through", None, reason, started, actor)
            return None
        render = candidates.get(name)
        if render is None:
            self._record("rejected", name, "tool_is_not_pickable", started, actor)
            return None
        try:
            validate_instance(self.tools.get(name).input_schema, arguments)
        except (SchemaError, KeyError) as error:
            self._record(
                "rejected", name, f"invalid_arguments: {error}"[:160], started, actor
            )
            return None
        result = self.tools.invoke(
            name, arguments, cancel_event=cancel_event, actor=actor
        )
        if result.status is ToolStatus.REJECTED:
            self._record("rejected", name, "registry_rejected", started, actor)
            return None
        reply = PickedReply(
            intent=f"pick:{name}",
            tool=name,
            result=result,
            spoken=render(result, language),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
        self._record("picked", name, result.status.value, started, actor)
        return reply

    def _candidates(self) -> dict[str, ReplyRenderer]:
        registered = set(self.tools.names())
        return {
            name: render
            for name, render in self.pickable.items()
            if name in registered
        }

    def _catalogue(self, candidates: Mapping[str, ReplyRenderer]) -> str:
        lines = []
        for schema in self.tools.schemas():
            name = schema["name"]
            if name not in candidates:
                continue
            description = str(schema["description"]).split(";")[0].strip()
            arguments = _signature(schema["input_schema"])
            lines.append(f"{name}({arguments}) - {description}")
        return "\n".join(lines)

    @staticmethod
    def _looks_tool_shaped(normalized: str) -> bool:
        if not _MINIMUM_LENGTH <= len(normalized) <= _MAXIMUM_LENGTH:
            return False
        if any(word in normalized for word in _REASONING_WORDS):
            return False
        return any(word in normalized for word in _TOOL_SHAPED_WORDS)

    def _record(
        self,
        status: str,
        tool: str | None,
        reason: str,
        started: float,
        actor: Actor,
    ) -> None:
        self.audit_sink.record(
            audit_event(
                "tool_pick",
                tool=tool,
                status=status,
                reason=reason,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                actor=actor.actor_id,
                actor_source=actor.source,
            )
        )
