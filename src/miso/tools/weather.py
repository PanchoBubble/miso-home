"""Bounded Open-Meteo weather lookup exposed as one validated tool."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry, ToolRejected


GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
MAX_RESPONSE_BYTES = 512 * 1024


class WeatherTransport(Protocol):
    def __call__(self, url: str, timeout_seconds: float) -> bytes: ...


@dataclass(frozen=True, slots=True)
class WeatherConfig:
    default_location: str | None = None
    request_timeout_seconds: float = 6.0
    cache_seconds: float = 600.0

    def __post_init__(self) -> None:
        location = self.default_location
        if location is not None and (
            not location.strip()
            or len(location) > 120
            or any(ord(character) < 32 for character in location)
        ):
            raise ValueError("weather default location is invalid")
        if (
            not math.isfinite(self.request_timeout_seconds)
            or not 0 < self.request_timeout_seconds <= 20
        ):
            raise ValueError("weather request timeout must be between 0 and 20 seconds")
        if not math.isfinite(self.cache_seconds) or not 0 <= self.cache_seconds <= 3600:
            raise ValueError("weather cache must be between 0 and 3600 seconds")


class OpenMeteoWeatherAdapter:
    """Resolve a named place and return a compact current/daily forecast."""

    def __init__(
        self,
        config: WeatherConfig,
        *,
        transport: WeatherTransport | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.transport = transport or _https_json_get
        self.now = now
        self._cache: dict[tuple[str, int, str, str], tuple[float, dict[str, object]]] = {}
        self._lock = threading.Lock()

    def weather_get(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        raw_location = arguments.get("location", self.config.default_location)
        if not isinstance(raw_location, str) or not raw_location.strip():
            raise ToolRejected("location is required because no home location is configured")
        location = raw_location.strip()
        if any(ord(character) < 32 for character in location):
            raise ToolRejected("location contains invalid characters")
        forecast_days = int(arguments.get("forecast_days", 1))
        units = str(arguments.get("units", "metric"))
        language = str(arguments.get("language", "en"))
        cache_key = (location.casefold(), forecast_days, units, language)
        cached = self._cached(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

        context.raise_if_cancelled()
        timeout = min(self.config.request_timeout_seconds, context.remaining_seconds())
        try:
            place = self._geocode(location, language, timeout)
            context.raise_if_cancelled()
            timeout = min(self.config.request_timeout_seconds, context.remaining_seconds())
            result = self._forecast(place, forecast_days, units, language, timeout)
        except ToolRejected:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ToolRejected("weather service is temporarily unavailable") from error
        context.raise_if_cancelled()
        result["cached"] = False
        with self._lock:
            self._cache[cache_key] = (self.now(), dict(result))
        return result

    def _cached(self, key: tuple[str, int, str, str]) -> dict[str, object] | None:
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None
            stored_at, value = item
            if self.now() - stored_at >= self.config.cache_seconds:
                self._cache.pop(key, None)
                return None
            return dict(value)

    def _geocode(
        self, location: str, language: str, timeout: float
    ) -> dict[str, object]:
        query = urlencode(
            {"name": location, "count": 1, "language": language, "format": "json"}
        )
        payload = _json_object(self.transport(f"{GEOCODING_ENDPOINT}?{query}", timeout))
        results = payload.get("results")
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)) or not results:
            raise ToolRejected("weather location was not found")
        candidate = results[0]
        if not isinstance(candidate, Mapping):
            raise ValueError("invalid geocoding result")
        name = _text(candidate, "name")
        latitude = _number(candidate, "latitude")
        longitude = _number(candidate, "longitude")
        timezone = _text(candidate, "timezone")
        country = candidate.get("country")
        admin1 = candidate.get("admin1")
        return {
            "name": name,
            "country": country if isinstance(country, str) else "",
            "admin1": admin1 if isinstance(admin1, str) else "",
            "latitude": latitude,
            "longitude": longitude,
            "timezone": timezone,
        }

    def _forecast(
        self,
        place: Mapping[str, object],
        forecast_days: int,
        units: str,
        language: str,
        timeout: float,
    ) -> dict[str, object]:
        parameters = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": (
                "temperature_2m,apparent_temperature,weather_code,"
                "precipitation,wind_speed_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max"
            ),
            "forecast_days": forecast_days,
            "timezone": "auto",
            "temperature_unit": "fahrenheit" if units == "imperial" else "celsius",
            "wind_speed_unit": "mph" if units == "imperial" else "kmh",
            "precipitation_unit": "inch" if units == "imperial" else "mm",
        }
        payload = _json_object(
            self.transport(f"{FORECAST_ENDPOINT}?{urlencode(parameters)}", timeout)
        )
        current = _mapping(payload, "current")
        current_units = _mapping(payload, "current_units")
        daily = _mapping(payload, "daily")
        daily_units = _mapping(payload, "daily_units")
        current_weather_code = int(_number(current, "weather_code"))
        current_result = {
            "observed_at": _text(current, "time"),
            "conditions": _condition(current_weather_code, language),
            "weather_code": current_weather_code,
            "temperature": _number(current, "temperature_2m"),
            "apparent_temperature": _number(current, "apparent_temperature"),
            "precipitation": _number(current, "precipitation"),
            "wind_speed": _number(current, "wind_speed_10m"),
            "temperature_unit": _text(current_units, "temperature_2m"),
            "precipitation_unit": _text(current_units, "precipitation"),
            "wind_speed_unit": _text(current_units, "wind_speed_10m"),
        }
        dates = _list(daily, "time", forecast_days)
        codes = _list(daily, "weather_code", forecast_days)
        highs = _list(daily, "temperature_2m_max", forecast_days)
        lows = _list(daily, "temperature_2m_min", forecast_days)
        precipitation = _list(daily, "precipitation_probability_max", forecast_days)
        forecast = []
        for index, date in enumerate(dates):
            code = int(_finite_number(codes[index]))
            forecast.append(
                {
                    "date": str(date),
                    "conditions": _condition(code, language),
                    "weather_code": code,
                    "temperature_max": _finite_number(highs[index]),
                    "temperature_min": _finite_number(lows[index]),
                    "precipitation_probability_max": _finite_number(
                        precipitation[index]
                    ),
                    "temperature_unit": _text(daily_units, "temperature_2m_max"),
                    "precipitation_probability_unit": _text(
                        daily_units, "precipitation_probability_max"
                    ),
                }
            )
        place_result = {
            "name": place["name"],
            "admin1": place["admin1"],
            "country": place["country"],
            "latitude": round(float(place["latitude"]), 4),
            "longitude": round(float(place["longitude"]), 4),
            "timezone": _text(payload, "timezone"),
        }
        return {
            "location": place_result,
            "current": current_result,
            "forecast": forecast,
            "summary": _summary(place_result, current_result, forecast[0], language),
            "attribution": ATTRIBUTION,
            "attribution_url": ATTRIBUTION_URL,
        }


def weather_tool_definition(adapter: OpenMeteoWeatherAdapter) -> ToolDefinition:
    properties: dict[str, object] = {
        "location": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "description": "City or place name, such as London or Madrid",
        },
        "forecast_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 7,
            "description": "Number of forecast days including today",
        },
        "units": {"type": "string", "enum": ["metric", "imperial"]},
        "language": {"type": "string", "enum": ["en", "es"]},
    }
    required = [] if adapter.config.default_location else ["location"]
    return ToolDefinition(
        name="weather_get",
        description=(
            "Get current weather and a short forecast for a real location using "
            "Open-Meteo; consulta el tiempo y el pronóstico. Use language es for "
            "Spanish requests."
        ),
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        handler=adapter.weather_get,
        timeout_seconds=8,
        redact_fields=frozenset({"location"}),
    )


def register_weather_tools(
    registry: ToolRegistry,
    config: WeatherConfig,
    *,
    transport: WeatherTransport | None = None,
    now: Callable[[], float] = time.monotonic,
) -> OpenMeteoWeatherAdapter:
    adapter = OpenMeteoWeatherAdapter(config, transport=transport, now=now)
    registry.register(weather_tool_definition(adapter))
    return adapter


def _https_json_get(url: str, timeout_seconds: float) -> bytes:
    if not url.startswith((f"{GEOCODING_ENDPOINT}?", f"{FORECAST_ENDPOINT}?")):
        raise ValueError("weather request endpoint is not allowlisted")
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Miso/0.1 weather tool"},
    )
    with urlopen(request, timeout=max(0.1, timeout_seconds)) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("weather response is too large")
    return body


def _json_object(body: bytes) -> Mapping[str, object]:
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("weather response is too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("weather service returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("weather service returned an invalid object")
    return payload


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    result = value.get(key)
    if not isinstance(result, Mapping):
        raise ValueError(f"weather response is missing {key}")
    return result


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"weather response is missing {key}")
    return result


def _number(value: Mapping[str, object], key: str) -> float:
    return _finite_number(value.get(key))


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("weather response contains an invalid number")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise ValueError("weather response contains a non-finite number")
    return result


def _list(value: Mapping[str, object], key: str, minimum: int) -> Sequence[object]:
    result = value.get(key)
    if (
        not isinstance(result, Sequence)
        or isinstance(result, (str, bytes))
        or len(result) < minimum
    ):
        raise ValueError(f"weather response is missing {key}")
    return result


_CONDITIONS = {
    0: ("clear skies", "cielo despejado"),
    1: ("mainly clear skies", "cielo mayormente despejado"),
    2: ("partly cloudy skies", "cielo parcialmente nublado"),
    3: ("overcast skies", "cielo cubierto"),
    45: ("fog", "niebla"),
    48: ("freezing fog", "niebla helada"),
    51: ("light drizzle", "llovizna ligera"),
    53: ("drizzle", "llovizna"),
    55: ("heavy drizzle", "llovizna intensa"),
    56: ("light freezing drizzle", "llovizna helada ligera"),
    57: ("freezing drizzle", "llovizna helada"),
    61: ("light rain", "lluvia ligera"),
    63: ("rain", "lluvia"),
    65: ("heavy rain", "lluvia intensa"),
    66: ("light freezing rain", "lluvia helada ligera"),
    67: ("freezing rain", "lluvia helada"),
    71: ("light snow", "nieve ligera"),
    73: ("snow", "nieve"),
    75: ("heavy snow", "nieve intensa"),
    77: ("snow grains", "granos de nieve"),
    80: ("light rain showers", "chubascos ligeros"),
    81: ("rain showers", "chubascos"),
    82: ("heavy rain showers", "chubascos intensos"),
    85: ("light snow showers", "chubascos de nieve ligeros"),
    86: ("heavy snow showers", "chubascos de nieve intensos"),
    95: ("thunderstorms", "tormentas"),
    96: ("thunderstorms with light hail", "tormentas con granizo ligero"),
    99: ("thunderstorms with heavy hail", "tormentas con granizo intenso"),
}


def _condition(code: int, language: str) -> str:
    descriptions = _CONDITIONS.get(code, ("unknown conditions", "condiciones desconocidas"))
    return descriptions[1 if language == "es" else 0]


def _format_number(value: object) -> str:
    number = _finite_number(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _summary(
    place: Mapping[str, object],
    current: Mapping[str, object],
    today: Mapping[str, object],
    language: str,
) -> str:
    name = str(place["name"])
    temperature = _format_number(current["temperature"])
    temperature_unit = str(current["temperature_unit"])
    high = _format_number(today["temperature_max"])
    low = _format_number(today["temperature_min"])
    rain = _format_number(today["precipitation_probability_max"])
    if language == "es":
        return (
            f"En {name} ahora hay {current['conditions']}, con {temperature}{temperature_unit}. "
            f"Hoy se espera una máxima de {high}{temperature_unit}, una mínima de "
            f"{low}{temperature_unit} y hasta un {rain}% de probabilidad de precipitación. "
            f"{ATTRIBUTION}."
        )
    return (
        f"In {name}, it is currently {current['conditions']} at {temperature}{temperature_unit}. "
        f"Today's high is {high}{temperature_unit}, the low is {low}{temperature_unit}, "
        f"with up to a {rain}% chance of precipitation. {ATTRIBUTION}."
    )
