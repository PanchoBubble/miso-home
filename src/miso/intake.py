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
    lowered = re.sub(r"[¿¡?!.;:]+", " ", text.casefold())
    # Commas survive because they separate the items of one spoken list;
    # a trailing one is punctuation rather than a separator.
    lowered = re.sub(r"\s*,\s*", ", ", lowered)
    lowered = re.sub(r",+\s*$", " ", lowered)
    lowered = re.sub(
        r"^(please|hey|oye|por favor|can you|could you|puedes|podrías|podrias)[\s,]+",
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
    for match in _HALF_PATTERN.finditer(text):
        total += 1800 if "hora" in match.group(0) or "hour" in match.group(0) else 30
    text = _HALF_PATTERN.sub("", text)
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
    if not 1 <= total <= 604_800:
        return None
    return total


def _match_timer_create(text: str, language: str) -> Mapping[str, object] | None:
    patterns = (
        r"(?:set|start) (?:a |an |the )?(?:(?P<title>.+?) )?timer (?:for )?(?P<duration>.+)",
        r"(?:set|start) (?:a |an )?(?P<duration>.+?) timer(?: (?:called|named|for) (?P<title>.+))?",
        r"(?:pon|ponme|inicia|crea) (?:un |el )?temporizador(?: (?:para|llamado) (?P<title>.+?))? (?:de|por) (?P<duration>.+)",
    )
    for pattern in patterns:
        found = re.fullmatch(pattern, text)
        if found is None:
            continue
        duration_text = found.group("duration")
        title = found.groupdict().get("title")
        named = re.fullmatch(r"(.+?) (?:called|named|para) (.+)", duration_text)
        if named and title is None:
            duration_text, title = named.groups()
        duration = _strict_duration(duration_text)
        if duration is not None:
            return {"duration_seconds": duration, **({"title": title} if title else {})}
    return None


def _strict_duration(text: str) -> int | None:
    rest = _DURATION_PATTERN.sub("", _HALF_PATTERN.sub("", text))
    if re.sub(r"\b(?:and|y)\b|[\s,]+", "", rest):
        return None
    return _parse_duration_seconds(text)


def _timer_target(text: str | None) -> str | None:
    if not text or text in {"it", "that", "the timer", "my timer", "timer", "el temporizador", "temporizador"}:
        return None
    text = re.sub(r"^(?:the|my|el) ", "", text)
    text = re.sub(r" timer$|^temporizador (?:de |para )?", "", text)
    return text.strip() or None


def _match_timer_control(text: str, language: str) -> Mapping[str, object] | None:
    action = None
    target = None
    seconds = None
    found = re.fullmatch(r"(?:add|give it) (.+?)(?: (?:to|on) (.+))?", text)
    spanish = re.fullmatch(r"(?:añade|agrega|suma) (.+?)(?: (?:al|a el) (.+))?", text)
    if found or spanish:
        duration, target = (found or spanish).groups()
        seconds = _strict_duration(re.sub(r"\b(?:more|más)\s*", "", duration).strip())
        if seconds is not None:
            action = "extend"
    if action is None:
        found = re.fullmatch(r"(?:cancel|stop) (.+?timer|timer)|(?:cancela|para) (?:el )?(temporizador(?: .+)?)", text)
        if found:
            action = "cancel"
            target = next(value for value in found.groups() if value is not None)
    if action is None:
        found = re.fullmatch(r"(?:how long|how much time)(?: is)? (?:left|remaining)(?: on (.+))?|cu[aá]nto (?:queda|falta)(?: (?:en|al) (.+))?", text)
        if found:
            target = next((value for value in found.groups() if value is not None), None)
            # Preserve the existing all-timers query for an unnamed timer.
            if target and _timer_target(target) is None:
                return None
            action = "remaining"
    if action is None:
        return None
    title = _timer_target(target)
    return {"action": action, **({"title": title} if title else {}),
            **({"seconds": seconds} if seconds is not None else {})}


def _render_timer_control(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    output = result.output or {}
    outcome = output.get("outcome")
    if outcome == "ambiguous":
        choices = ", ".join(str(item) for item in output.get("choices", [])[:5])
        return (f"¿Qué temporizador? {choices}." if language == "es"
                else f"Which timer? {choices}.")
    if outcome == "not_found":
        return "No encuentro ese temporizador activo." if language == "es" else "I couldn't find a matching running timer."
    if outcome == "changed":
        return "El temporizador ha cambiado. Inténtalo de nuevo." if language == "es" else "That timer has changed. Please try again."
    timer = output.get("timer", {})
    title = str(timer.get("title", "Timer"))
    if output.get("action") == "cancel":
        return f"He cancelado {title}." if language == "es" else f"Cancelled {title}."
    remaining = _describe_duration(_seconds_until(timer.get("due_at")), language)
    return f"{title}: quedan {remaining}." if language == "es" else f"{title}: {remaining} left."


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
    title = timer.get("title") if isinstance(timer, Mapping) else None
    if title and title != "Timer":
        return (f"{title}: temporizador de {described} en marcha." if language == "es"
                else f"{title} timer set for {described}.")
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
        label = str(entry.get("title", "Timer"))
        described.append(f"{label}: {_describe_duration(remaining, language)}")
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


# Which list a request means. Transcription drops tildes often enough that
# every Spanish verb has to accept the bare "n" spelling, and "the list" on
# its own is the shopping list as far as the household is concerned.
_EN_LIST = (
    r"(?:the\s+|my\s+|our\s+)?(?:shopping|grocery|groceries|food)?\s*list"
)
_ES_LIST = (
    r"(?:la\s+lista(?:\s+de\s+(?:la\s+)?compras?)?"
    r"|la\s+compra|las\s+compras|el\s+s[uú]per(?:mercado)?)"
)
_ES_LIST_OF = r"(?:de\s+" + _ES_LIST + r"|del\s+s[uú]per(?:mercado)?)"
_COURTESY = r"(?:\s+please|\s+por\s+favor|\s+gracias)?"

_SHOPPING_ADD_PATTERNS = (
    re.compile(
        r"^(?:add|put|stick)\s+(?P<item>.+?)\s+(?:to|on|onto)\s+"
        + _EN_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:a[ñn]ade|a[ñn]adir|agrega|agregar|apunta|apuntar|pon|poner|mete|"
        r"meter|incluye|suma)\s+(?P<item>.+?)\s+(?:a|en)\s+"
        + _ES_LIST + _COURTESY + r"$"
    ),
)
_SHOPPING_REMOVE_PATTERNS = (
    re.compile(
        r"^(?:remove|delete|drop)\s+(?P<item>.+?)\s+(?:from|off)\s+(?:of\s+)?"
        + _EN_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:take|cross|scratch|tick|check)\s+(?P<item>.+?)\s+off\s+(?:of\s+)?"
        + _EN_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:quita|quitar|borra|borrar|elimina|eliminar|saca|sacar|tacha|"
        r"tachar)\s+(?P<item>.+?)\s+" + _ES_LIST_OF + _COURTESY + r"$"
    ),
)
_SHOPPING_LIST_PATTERNS = (
    re.compile(
        r"^what(?:'s| is| are)\s+(?:on|in)\s+" + _EN_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:read|show|list|tell)\s+(?:me\s+)?(?:out\s+)?"
        + _EN_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:qu[eé]|cu[aá]les)\s+(?:hay|est[aá]n?|tenemos|falta|faltan)\s+"
        r"(?:en|de)\s+" + _ES_LIST + _COURTESY + r"$"
    ),
    re.compile(
        r"^(?:lee|leeme|l[eé]eme|muestra|mu[eé]strame|dime|dame|ens[eé][ñn]ame)"
        r"\s+" + _ES_LIST + _COURTESY + r"$"
    ),
)

# Articles a spoken item name carries but the stored item should not, so
# "quita la leche" removes the same row "añade leche" created.
_ITEM_ARTICLE = re.compile(
    r"^(?:the|a|an|some|el|la|los|las|unos|unas|algo\s+de)\s+(?=\S)"
)
# One utterance often names several items. Splitting here keeps each one its
# own row, which is what the dashboard and a later removal both need.
_ITEM_SEPARATOR = re.compile(r"\s*,\s*|\s+and\s+|\s+y\s+|\s+e\s+")
_ITEM_QUANTITY = re.compile(
    r"^(?P<quantity>\d{1,3}|"
    + "|".join(re.escape(word) for word in _NUMBER_WORDS)
    + r")\s+(?P<rest>\S.*)$"
)


def _clean_item(item: str) -> str | None:
    cleaned = _ITEM_ARTICLE.sub("", item.strip()).strip()
    if not 1 <= len(cleaned) <= 120:
        return None
    return cleaned


def _split_quantity(item: str) -> tuple[str, int]:
    """Peel a leading count off an item name, leaving the name speakable."""
    found = _ITEM_QUANTITY.match(item)
    if found is None:
        return item, 1
    quantity_text = found.group("quantity")
    quantity = (
        int(quantity_text)
        if quantity_text.isdigit()
        else _NUMBER_WORDS[quantity_text]
    )
    rest = _clean_item(found.group("rest"))
    if rest is None or not 1 <= quantity <= 999:
        return item, 1
    return rest, quantity


def _parse_shopping_items(text: str) -> list[dict[str, object]] | None:
    """Parse an add request into one entry per item, or None if it is not one."""
    for pattern in _SHOPPING_ADD_PATTERNS:
        found = pattern.match(text)
        if found is None:
            continue
        entries: list[dict[str, object]] = []
        for part in _ITEM_SEPARATOR.split(found.group("item")):
            item = _clean_item(part)
            if item is None:
                return None
            name, quantity = _split_quantity(item)
            entry: dict[str, object] = {"name": name}
            if quantity > 1:
                entry["quantity"] = quantity
            entries.append(entry)
        if not 1 <= len(entries) <= 20:
            return None
        return entries
    return None


def _match_shopping_add(text: str, language: str) -> Mapping[str, object] | None:
    entries = _parse_shopping_items(text)
    if entries is None or len(entries) != 1:
        return None
    return entries[0]


def _match_shopping_add_many(text: str, language: str) -> Mapping[str, object] | None:
    entries = _parse_shopping_items(text)
    if entries is None or len(entries) < 2:
        return None
    return {"items": entries}


def _shopping_label(result: ToolResult, language: str) -> tuple[str, int]:
    item = (result.output or {}).get("item")
    name = item.get("name") if isinstance(item, Mapping) else None
    quantity = item.get("quantity") if isinstance(item, Mapping) else None
    label = str(name) if isinstance(name, str) and name else (
        "el artículo" if language == "es" else "the item"
    )
    return label, quantity if isinstance(quantity, int) and quantity > 1 else 1


def _describe_item(item: object, language: str) -> str:
    name = item.get("name") if isinstance(item, Mapping) else None
    quantity = item.get("quantity") if isinstance(item, Mapping) else None
    label = str(name) if isinstance(name, str) and name else (
        "el artículo" if language == "es" else "the item"
    )
    if isinstance(quantity, int) and quantity > 1:
        return f"{quantity} {label}"
    return label


def _render_shopping_add(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    added = (result.output or {}).get("items")
    entries = added if isinstance(added, list) else []
    labels = [
        _describe_item(entry, language)
        for entry in entries
        if isinstance(entry, Mapping)
    ]
    if not labels:
        labels = [_describe_item((result.output or {}).get("item"), language)]
    joiner = " y " if language == "es" else " and "
    listed = (
        joiner.join(labels)
        if len(labels) < 3
        else ", ".join(labels[:-1]) + joiner + labels[-1]
    )
    return f"He añadido {listed}." if language == "es" else f"Added {listed}."


def _match_shopping_remove(text: str, language: str) -> Mapping[str, object] | None:
    for pattern in _SHOPPING_REMOVE_PATTERNS:
        found = pattern.match(text)
        if found is None:
            continue
        item = _clean_item(found.group("item"))
        if item is None:
            return None
        name, _ = _split_quantity(item)
        return {"name": name}
    return None


def _render_shopping_remove(result: ToolResult, language: str) -> str:
    if not result.ok:
        return _failure_phrase(language)
    output = result.output or {}
    if output.get("outcome") == "ambiguous":
        return ("Hay varios artículos que coinciden. Dime el nombre completo."
                if language == "es" else "Several items match. Please say the full item name.")
    if output.get("removed") is False:
        asked = output.get("name")
        label = str(asked) if isinstance(asked, str) and asked else (
            "eso" if language == "es" else "that"
        )
        spoken = label[:1].upper() + label[1:]
        if language == "es":
            return f"{spoken} no está en la lista."
        return f"{spoken} isn't on the list."
    label, _ = _shopping_label(result, language)
    return f"He quitado {label}." if language == "es" else f"Removed {label}."


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
        _describe_item(entry, language)
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
    r"[¿¡ñ]|\b(qué|que|cuánto|cuanto|añade|anade|agrega|apunta|mete|incluye|"
    r"quita|borra|elimina|saca|tacha|compra|compras|súper|super|lista|"
    r"temporizador|tiempo|pon|hace|hay|para|minutos?|horas?|segundos?)\b"
)


def guess_language(text: str) -> str:
    """Crude typed-text language hint for rendering fast-lane replies."""
    return "es" if _SPANISH_MARKERS.search(text.casefold()) else "en"


def default_intents() -> tuple[FastIntent, ...]:
    return (
        FastIntent("timer_control", "timer_control", _match_timer_control, _render_timer_control),
        FastIntent("timer_create", "timer_create", _match_timer_create, _render_timer_create),
        FastIntent("timer_list", "timer_list", _match_timer_list, _render_timer_list),
        FastIntent("shopping_add", "shopping_add", _match_shopping_add, _render_shopping_add),
        FastIntent(
            "shopping_add_many",
            "shopping_add_many",
            _match_shopping_add_many,
            _render_shopping_add,
        ),
        FastIntent(
            "shopping_remove",
            "shopping_remove",
            _match_shopping_remove,
            _render_shopping_remove,
        ),
        FastIntent("shopping_list", "shopping_list", _match_shopping_list, _render_shopping_list),
        FastIntent("weather_get", "weather_get", _match_weather, _render_weather),
        FastIntent(
            "tools_refresh", "tools_refresh", _match_tools_refresh, _render_tools_refresh
        ),
    )


def match_fast_intent(
    text: str,
    language: str,
    intents: tuple[FastIntent, ...] | None = None,
) -> tuple[str, Mapping[str, object]] | None:
    """Report which intent an utterance takes, without invoking its tool.

    Offline scoring needs to know whether a transcript still reaches the fast
    lane, and it must not create timers or shopping items to find out.
    """
    normalized = _normalize(text)
    if not normalized:
        return None
    for intent in default_intents() if intents is None else intents:
        arguments = intent.match(normalized, language)
        if arguments is not None:
            return intent.name, arguments
    return None


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
        self._context_lock = threading.Lock()
        self._contexts: dict[tuple[str, str], tuple[float, str, dict, dict]] = {}

    def try_handle(
        self,
        text: str,
        language: str,
        *,
        cancel_event: threading.Event | None = None,
        actor: Actor = VOICE_ACTOR,
        conversation_id: str | None = None,
    ) -> FastReply | None:
        if not self.enabled:
            return None
        started = time.monotonic()
        normalized = _normalize(text)
        if not normalized:
            return None
        registered = set(self.tools.names())
        key = (actor.actor_id, conversation_id) if conversation_id else None
        previous = None
        if key:
            with self._context_lock:
                self._contexts = {k: v for k, v in self._contexts.items() if v[0] > started}
                previous = self._contexts.get(key)
        selected = None
        if previous:
            _, previous_tool, previous_arguments, output = previous
            if normalized in {"cancel it", "stop it", "cancélalo", "cancelalo"}:
                timer = output.get("timer")
                if isinstance(timer, Mapping) and timer.get("status") == "pending":
                    selected = ("timer_control", {"action": "cancel", "title": timer["title"]})
            if output.get("outcome") == "ambiguous" and previous_tool == "timer_control":
                choice = _timer_target(normalized)
                if choice and choice in [str(item).casefold() for item in output.get("choices", [])]:
                    selected = (previous_tool, {**previous_arguments, "title": choice})
            if previous_tool.startswith("shopping_"):
                if output.get("outcome") == "ambiguous" and normalized in [
                    str(item).casefold() for item in output.get("choices", [])
                ]:
                    selected = (previous_tool, {**previous_arguments, "name": normalized})
                if re.fullmatch(r"(?:and|also|y|también) .+", normalized):
                    normalized = re.sub(r"^(?:and|also|y|también) ", "add ", normalized) + " to the shopping list"
                elif re.fullmatch(r"(?:remove|delete|quita|borra) .+", normalized) and not re.search(r"\b(?:list|lista|from|de)\b", normalized):
                    normalized += " de la lista" if language == "es" else " from the shopping list"
        for intent in self.intents:
            if intent.tool not in registered:
                continue
            arguments = (selected[1] if selected and selected[0] == intent.tool else
                         None if selected else intent.match(normalized, language))
            if arguments is None:
                continue
            if previous and intent.tool == "timer_control" and "title" not in arguments:
                timer = previous[3].get("timer")
                if isinstance(timer, Mapping) and timer.get("status") == "pending":
                    arguments = {**arguments, "title": timer["title"]}
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
            if key:
                with self._context_lock:
                    if len(self._contexts) >= 128:
                        self._contexts.pop(next(iter(self._contexts)))
                    self._contexts[key] = (time.monotonic() + 120, intent.tool,
                                           dict(arguments), dict(result.output or {}))
            return reply
        if key:
            with self._context_lock:
                self._contexts.pop(key, None)
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
