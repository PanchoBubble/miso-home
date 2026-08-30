"""Deterministic fast-lane intent matching that answers without a model.

The fast lane sits between transcription and routing. Each intent pairs a
strict bilingual parser with a templated spoken reply, so a matching request
invokes its tool directly and skips the model round-trip entirely. A parser
that is not fully confident must return None: the request then falls through
to the model lane unchanged. Guessing arguments here would silently do the
wrong thing at high speed, which is worse than being slow.
"""

from __future__ import annotations

import re
import time
import threading
from dataclasses import dataclass
from typing import Callable, Mapping

from miso.identity import Actor, VOICE_ACTOR
from miso.tools import ToolRegistry, ToolResult, ToolStatus
from miso.tools.audit import AuditSink, InMemoryAuditLog, audit_event


IntentMatcher = Callable[[str, str], Mapping[str, object] | None]
ReplyRenderer = Callable[[ToolResult, str], str]


@dataclass(frozen=True, slots=True)
class FastIntent:
    """A deterministic utterance parser bound to one allowlisted tool."""

    name: str
    tool: str
    match: IntentMatcher
    render: ReplyRenderer


@dataclass(frozen=True, slots=True)
class FastReply:
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


def _normalize(text: str) -> str:
    lowered = re.sub(r"[¿¡?!.,;:]+", " ", text.casefold())
    lowered = re.sub(
        r"^(please|hey|oye|por favor|can you|could you|puedes|podrías|podrias)\s+",
        "",
        lowered.strip(),
    )
    return " ".join(lowered.split())


_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50, "sixty": 60, "ninety": 90,
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "once": 11, "doce": 12, "quince": 15, "veinte": 20, "treinta": 30,
    "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "noventa": 90,
}

_UNIT_SECONDS = {
    "hour": 3600, "hours": 3600, "hora": 3600, "horas": 3600,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60,
    "minuto": 60, "minutos": 60,
    "second": 1, "seconds": 1, "sec": 1, "secs": 1,
    "segundo": 1, "segundos": 1,
}

_DURATION_PATTERN = re.compile(
    r"\b(\d{1,5}|" + "|".join(re.escape(word) for word in _NUMBER_WORDS) + r")"
    r"(?:\s+and\s+a\s+half|\s+y\s+media|\s+y\s+medio)?"
    r"\s+(" + "|".join(_UNIT_SECONDS) + r")\b"
)
_HALF_PATTERN = re.compile(
    r"\b(half an hour|half hour|media hora|medio minuto|half a minute)\b"
)
_TIMER_WORDS = ("timer", "temporizador", "cronómetro", "cronometro", "cuenta atrás", "cuenta atras")


def _parse_duration_seconds(text: str) -> int | None:
    total = 0
    for match in _DURATION_PATTERN.finditer(text):
        quantity_text, unit = match.group(1), match.group(2)
        quantity = (
            int(quantity_text)
            if quantity_text.isdigit()
            else _NUMBER_WORDS[quantity_text]
        )
        seconds = quantity * _UNIT_SECONDS[unit]
        if "half" in match.group(0) or "media" in match.group(0) or "medio" in match.group(0):
            seconds += _UNIT_SECONDS[unit] // 2
        total += seconds
    for match in _HALF_PATTERN.finditer(text):
        total += 1800 if "hora" in match.group(0) or "hour" in match.group(0) else 30
    if not 1 <= total <= 604_800:
        return None
    return total


def _match_timer_create(text: str, language: str) -> Mapping[str, object] | None:
    if not any(word in text for word in _TIMER_WORDS):
        return None
    if any(word in text for word in ("cancel", "cancela", "stop", "para", "list", "lista", "left", "queda", "quedan")):
        return None
    duration = _parse_duration_seconds(text)
    if duration is None:
        return None
    return {"duration_seconds": duration}


def _describe_duration(seconds: int, language: str) -> str:
    parts: list[str] = []
    units = (
        (3600, ("hour", "hours"), ("hora", "horas")),
        (60, ("minute", "minutes"), ("minuto", "minutos")),
        (1, ("second", "seconds"), ("segundo", "segundos")),
    )
    remaining = seconds
    for size, english, spanish in units:
        value, remaining = divmod(remaining, size)
        if value:
            names = spanish if language == "es" else english
            parts.append(f"{value} {names[0] if value == 1 else names[1]}")
    joiner = " y " if language == "es" else " and "
    return joiner.join(parts) if parts else ("0 segundos" if language == "es" else "0 seconds")


def _render_timer_create(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    timer = (result.output or {}).get("timer")
    seconds = 0
    if isinstance(timer, Mapping):
        # due_at/created_at are ISO strings; the requested duration is not
        # echoed back, so recover it from the stored timestamps.
        seconds = _seconds_between(timer.get("created_at"), timer.get("due_at"))
    described = _describe_duration(seconds, language)
    if language == "es":
        return f"Temporizador de {described} en marcha."
    return f"Timer set for {described}."


def _seconds_between(start: object, end: object) -> int:
    from datetime import datetime

    try:
        started = datetime.fromisoformat(str(start))
        due = datetime.fromisoformat(str(end))
    except (TypeError, ValueError):
        return 0
    return max(0, round((due - started).total_seconds()))


def _match_timer_list(text: str, language: str) -> Mapping[str, object] | None:
    if not any(word in text for word in _TIMER_WORDS):
        return None
    if any(
        phrase in text
        for phrase in (
            "list", "what timer", "which timer", "how long", "how much",
            "left on", "remaining", "lista", "qué temporizador",
            "que temporizador", "cuánto queda", "cuanto queda", "cuánto falta",
            "cuanto falta",
        )
    ):
        return {"status": "pending"}
    return None


def _render_timer_list(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    timers = (result.output or {}).get("timers")
    entries = timers if isinstance(timers, list) else []
    if not entries:
        return "No hay temporizadores activos." if language == "es" else "No timers are running."
    described: list[str] = []
    for entry in entries[:4]:
        if not isinstance(entry, Mapping):
            continue
        remaining = _seconds_until(entry.get("due_at"))
        described.append(_describe_duration(remaining, language))
    if language == "es":
        return "Temporizadores: " + ", ".join(f"quedan {item}" for item in described) + "."
    return "Timers: " + ", ".join(f"{item} left" for item in described) + "."


def _seconds_until(due: object) -> int:
    from datetime import datetime, timezone

    try:
        deadline = datetime.fromisoformat(str(due))
    except (TypeError, ValueError):
        return 0
    now = datetime.now(deadline.tzinfo or timezone.utc)
    return max(0, round((deadline - now).total_seconds()))


_SHOPPING_ADD_PATTERNS = (
    re.compile(r"^(?:add|put)\s+(?P<item>.+?)\s+(?:to|on)\s+(?:the\s+|my\s+)?shopping\s+list$"),
    re.compile(r"^(?:añade|agrega|apunta|pon)\s+(?P<item>.+?)\s+(?:a|en)\s+la\s+lista(?:\s+de\s+la\s+compra|\s+de\s+compras?)?$"),
)
_SHOPPING_LIST_PATTERNS = (
    re.compile(r"^what(?:'s| is)\s+on\s+(?:the\s+|my\s+)?shopping\s+list$"),
    re.compile(r"^(?:read|show|list)\s+(?:me\s+)?(?:the\s+|my\s+)?shopping\s+list$"),
    re.compile(r"^qué\s+hay\s+en\s+la\s+lista(?:\s+de\s+la\s+compra|\s+de\s+compras?)?$"),
    re.compile(r"^que\s+hay\s+en\s+la\s+lista(?:\s+de\s+la\s+compra|\s+de\s+compras?)?$"),
    re.compile(r"^(?:lee|muestra|dime)\s+la\s+lista(?:\s+de\s+la\s+compra|\s+de\s+compras?)?$"),
)


def _match_shopping_add(text: str, language: str) -> Mapping[str, object] | None:
    for pattern in _SHOPPING_ADD_PATTERNS:
        found = pattern.match(text)
        if found is None:
            continue
        item = found.group("item").strip()
        if not 1 <= len(item) <= 120:
            return None
        return {"name": item}
    return None


def _render_shopping_add(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    item = (result.output or {}).get("item")
    name = item.get("name") if isinstance(item, Mapping) else None
    label = str(name) if isinstance(name, str) and name else (
        "el artículo" if language == "es" else "the item"
    )
    return f"He añadido {label}." if language == "es" else f"Added {label}."


def _match_shopping_list(text: str, language: str) -> Mapping[str, object] | None:
    if any(pattern.match(text) for pattern in _SHOPPING_LIST_PATTERNS):
        return {}
    return None


def _render_shopping_list(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    items = (result.output or {}).get("items")
    entries = items if isinstance(items, list) else []
    names = [
        str(entry["name"])
        for entry in entries
        if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
    ]
    if not names:
        return "La lista de la compra está vacía." if language == "es" else "The shopping list is empty."
    listed = ", ".join(names[:10])
    overflow = len(names) - 10
    if overflow > 0:
        listed += f" y {overflow} más" if language == "es" else f" and {overflow} more"
    if language == "es":
        return f"En la lista: {listed}."
    return f"On the list: {listed}."


_WEATHER_WORDS = (
    "weather", "forecast", "tiempo hace", "qué tiempo", "que tiempo",
    "clima", "pronóstico", "pronostico", "va a llover", "will it rain",
)
_WEATHER_LOCATION = re.compile(
    r"\b(?:in|en)\s+(?P<place>[\wáéíóúüñ][\wáéíóúüñ' -]{1,80})$"
)


def _match_weather(text: str, language: str) -> Mapping[str, object] | None:
    if not any(word in text for word in _WEATHER_WORDS):
        return None
    arguments: dict[str, object] = {"language": "es" if language == "es" else "en"}
    location = _WEATHER_LOCATION.search(text)
    if location is not None:
        place = location.group("place").strip()
        if place not in ("the morning", "la mañana", "la manana", "the evening", "la tarde"):
            arguments["location"] = place
    return arguments


def _render_weather(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    summary = result.summary
    if summary:
        return summary
    return _failure_phrase(language)


_TOOL_REFRESH_PATTERNS = (
    re.compile(r"^(?:refresh|reload|update)\s+(?:your\s+|the\s+|my\s+)?tools?(?:\s+list|\s+modules?)?$"),
    re.compile(r"^(?:refresh|reload)\s+(?:the\s+)?tool\s+(?:list|registry|modules?)$"),
    re.compile(r"^(?:recarga|refresca|actualiza)\s+(?:las\s+|tus\s+|mis\s+)?herramientas$"),
    re.compile(r"^(?:recarga|refresca|actualiza)\s+(?:la\s+)?lista\s+de\s+herramientas$"),
)


def _match_tools_refresh(text: str, language: str) -> Mapping[str, object] | None:
    if any(pattern.match(text) for pattern in _TOOL_REFRESH_PATTERNS):
        return {}
    return None


def _render_tools_refresh(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    output = result.output or {}
    counts = [
        (key, len(value))
        for key, value in (
            ("added", output.get("added")),
            ("updated", output.get("updated")),
            ("removed", output.get("removed")),
        )
        if isinstance(value, list) and value
    ]
    labels = {
        "added": ("added", "nuevas"),
        "updated": ("updated", "actualizadas"),
        "removed": ("removed", "retiradas"),
    }
    described = ", ".join(
        f"{count} {labels[key][1 if language == 'es' else 0]}" for key, count in counts
    )
    if language == "es":
        spoken = (
            f"Herramientas recargadas: {described}."
            if described
            else "Herramientas recargadas, sin cambios."
        )
    else:
        spoken = (
            f"Tools reloaded: {described}."
            if described
            else "Tools reloaded, nothing changed."
        )
    failed = output.get("failed")
    if isinstance(failed, list) and failed:
        modules = ", ".join(
            str(entry.get("module"))
            for entry in failed
            if isinstance(entry, Mapping) and entry.get("module")
        )
        if language == "es":
            return f"{spoken} Rechacé estos módulos: {modules}."
        return f"{spoken} I rejected these modules: {modules}."
    return spoken


def _failure_phrase(language: str) -> str:
    return (
        "No he podido hacerlo, inténtalo de nuevo."
        if language == "es"
        else "I couldn't do that, please try again."
    )


_SPANISH_MARKERS = re.compile(
    r"[¿¡ñ]|\b(qué|que|cuánto|cuanto|añade|agrega|lista|temporizador|tiempo|"
    r"pon|hace|hay|para|minutos?|horas?|segundos?)\b"
)


def guess_language(text: str) -> str:
    """Crude typed-text language hint for rendering fast-lane replies."""
    return "es" if _SPANISH_MARKERS.search(text.casefold()) else "en"


def default_intents() -> tuple[FastIntent, ...]:
    return (
        FastIntent("timer_create", "timer_create", _match_timer_create, _render_timer_create),
        FastIntent("timer_list", "timer_list", _match_timer_list, _render_timer_list),
        FastIntent("shopping_add", "shopping_add", _match_shopping_add, _render_shopping_add),
        FastIntent("shopping_list", "shopping_list", _match_shopping_list, _render_shopping_list),
        FastIntent("weather_get", "weather_get", _match_weather, _render_weather),
        FastIntent(
            "tools_refresh", "tools_refresh", _match_tools_refresh, _render_tools_refresh
        ),
    )


class FastLane:
    """Try deterministic intents before any model call.

    Ownership rule: once an intent matches and its tool executes, the fast
    lane owns the turn, including failures, so a mutating tool can never run
    twice for one utterance. The single exception is a REJECTED result, which
    the registry produces before the handler runs: nothing happened, so the
    request falls through to the model lane.
    """

    def __init__(
        self,
        tools: ToolRegistry,
        audit_sink: AuditSink | None = None,
        intents: tuple[FastIntent, ...] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self.tools = tools
        self.audit_sink = audit_sink or InMemoryAuditLog()
        self.intents = default_intents() if intents is None else intents
        self.enabled = enabled

    def try_handle(
        self,
        text: str,
        language: str,
        *,
        cancel_event: threading.Event | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> FastReply | None:
        if not self.enabled:
            return None
        started = time.monotonic()
        normalized = _normalize(text)
        if not normalized:
            return None
        registered = set(self.tools.names())
        for intent in self.intents:
            if intent.tool not in registered:
                continue
            arguments = intent.match(normalized, language)
            if arguments is None:
                continue
            result = self.tools.invoke(
                intent.tool, arguments, cancel_event=cancel_event, actor=actor
            )
            if result.status is ToolStatus.REJECTED:
                self._record(intent, "rejected_fell_through", started, actor)
                return None
            reply = FastReply(
                intent=intent.name,
                tool=intent.tool,
                result=result,
                spoken=intent.render(result, language),
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
            self._record(intent, result.status.value, started, actor)
            return reply
        return None

    def _record(self, intent: FastIntent, status: str, started: float, actor: Actor) -> None:
        self.audit_sink.record(
            audit_event(
                "fast_intent",
                intent=intent.name,
                tool=intent.tool,
                status=status,
                duration_ms=max(0, round((time.monotonic() - started) * 1000)),
                actor=actor.actor_id,
                actor_source=actor.source,
            )
        )
