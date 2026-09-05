"""Bounded Open-Meteo weather lookup exposed as one validated tool."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from miso.memory import utc_now
from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry, ToolRejected


LOGGER = logging.getLogger("miso.weather")

GEOCODING_ENDPOINT = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
# Place names are resolved in one language so a Spanish and an English request
# for the same town share a cache entry and the same polled snapshot.
GEOCODING_LANGUAGE = "en"
ATTRIBUTION = "Weather data by Open-Meteo.com"
ATTRIBUTION_URL = "https://open-meteo.com/"
MAX_RESPONSE_BYTES = 512 * 1024
# A full week, so "what about Thursday" is answered from the snapshot rather
# than a live call. Open-Meteo returns seven days in the same single request.
POLL_FORECAST_DAYS = 7
MAX_FORECAST_DAYS = 7
DEFAULT_POLL_SECONDS = 900.0
MIN_POLL_SECONDS = 60.0
MAX_POLL_SECONDS = 86_400.0
# A snapshot older than this stops standing in for a live lookup, so a poller
# that has been failing quietly cannot answer with yesterday's rain.
STALE_SNAPSHOT_MULTIPLIER = 3.0


class WeatherTransport(Protocol):
    def __call__(self, url: str, timeout_seconds: float) -> bytes: ...


@dataclass(frozen=True, slots=True)
class WeatherConfig:
    default_location: str | None = None
    units: str = "metric"
    language: str = "en"
    request_timeout_seconds: float = 6.0
    cache_seconds: float = 600.0
    poll_seconds: float = DEFAULT_POLL_SECONDS

    def __post_init__(self) -> None:
        location = self.default_location
        if location is not None and (
            not location.strip()
            or len(location) > 120
            or any(ord(character) < 32 for character in location)
        ):
            raise ValueError("weather default location is invalid")
        if self.units not in {"metric", "imperial"}:
            raise ValueError("weather units must be metric or imperial")
        if self.language not in {"en", "es"}:
            raise ValueError("weather language must be en or es")
        if (
            not math.isfinite(self.request_timeout_seconds)
            or not 0 < self.request_timeout_seconds <= 20
        ):
            raise ValueError("weather request timeout must be between 0 and 20 seconds")
        if not math.isfinite(self.cache_seconds) or not 0 <= self.cache_seconds <= 3600:
            raise ValueError("weather cache must be between 0 and 3600 seconds")
        if not math.isfinite(self.poll_seconds) or self.poll_seconds < 0:
            raise ValueError("weather poll interval must not be negative")
        if self.poll_seconds and not MIN_POLL_SECONDS <= self.poll_seconds <= MAX_POLL_SECONDS:
            raise ValueError(
                "weather poll interval must be 0 or between 60 and 86400 seconds"
            )


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    """One polled forecast, held in memory and rendered per request."""

    payload: Mapping[str, object]
    location_key: str
    units: str
    forecast_days: int
    polled_at: float
    polled_at_utc: str


class WeatherSnapshotStore:
    """The household forecast the poller keeps warm, in memory only.

    Nothing here is written to disk: a restart simply polls again, and the
    snapshot holds public forecast data rather than anything about the house.
    """

    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self.now = now
        self._lock = threading.Lock()
        self._snapshot: WeatherSnapshot | None = None
        self._failure: str | None = None

    def store(
        self,
        payload: Mapping[str, object],
        *,
        location_key: str,
        units: str,
        forecast_days: int,
    ) -> WeatherSnapshot:
        snapshot = WeatherSnapshot(
            payload=dict(payload),
            location_key=location_key,
            units=units,
            forecast_days=forecast_days,
            polled_at=self.now(),
            polled_at_utc=utc_now(),
        )
        with self._lock:
            self._snapshot = snapshot
            self._failure = None
        return snapshot

    def current(self) -> tuple[WeatherSnapshot, float] | None:
        """Return the snapshot with its age, so callers judge staleness once."""
        with self._lock:
            snapshot = self._snapshot
        if snapshot is None:
            return None
        return snapshot, max(0.0, self.now() - snapshot.polled_at)

    def clear(self) -> None:
        """Forget the snapshot, because it describes the wrong place now."""
        with self._lock:
            self._snapshot = None
            self._failure = None

    def record_failure(self, reason: str) -> None:
        # The last good snapshot is kept: a stale forecast plus a visible
        # failure beats a blank panel during a short outage.
        with self._lock:
            self._failure = reason

    def failure(self) -> str | None:
        with self._lock:
            return self._failure


WEATHER_LOCATION_SETTING = "weather.location"


def validate_location(value: str) -> str:
    """Bound a caller-supplied place name the same way everywhere."""
    location = value.strip()
    if not location:
        raise ToolRejected("weather location must not be empty")
    if len(location) > 120 or any(ord(character) < 32 for character in location):
        raise ToolRejected("weather location is invalid")
    return location


class WeatherHome:
    """The household's weather location, changeable while Miso is running.

    The env file only seeds this. Once anyone sets a location from the
    dashboard or by voice it is stored in the database and wins, so a household
    can move Miso without editing a root-owned file or restarting the service.
    """

    def __init__(
        self,
        location: str | None = None,
        *,
        persist: Callable[[str | None], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._location = validate_location(location) if location else None
        self._persist = persist
        self._listeners: list[Callable[[str | None], None]] = []

    def add_listener(self, listener: Callable[[str | None], None]) -> None:
        """Register a change listener; each one is called after a set()."""
        self._listeners.append(listener)

    def location(self) -> str | None:
        with self._lock:
            return self._location

    def set(self, location: str | None) -> str | None:
        resolved = validate_location(location) if location else None
        with self._lock:
            if resolved == self._location:
                return resolved
            self._location = resolved
        if self._persist is not None:
            self._persist(resolved)
        for listener in self._listeners:
            try:
                listener(resolved)
            except Exception:
                # A screen or a poller failing to react must not undo a
                # setting the household just made.
                LOGGER.exception("weather home listener failed")
        return resolved


class OpenMeteoWeatherAdapter:
    """Resolve a named place and return a compact current/daily forecast."""

    def __init__(
        self,
        config: WeatherConfig,
        *,
        transport: WeatherTransport | None = None,
        now: Callable[[], float] = time.monotonic,
        snapshots: WeatherSnapshotStore | None = None,
        home: WeatherHome | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or _https_json_get
        self.now = now
        self.snapshots = snapshots
        self.home = home or WeatherHome(config.default_location)
        self._cache: dict[tuple[str, int, str], tuple[float, dict[str, object]]] = {}
        self._lock = threading.Lock()

    def weather_get(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        raw_location = arguments.get("location", self.home.location())
        if not isinstance(raw_location, str) or not raw_location.strip():
            raise ToolRejected("location is required because no home location is configured")
        location = validate_location(raw_location)
        day = int(arguments.get("day", 0))
        if not 0 <= day < MAX_FORECAST_DAYS:
            raise ToolRejected(f"day must be between 0 and {MAX_FORECAST_DAYS - 1}")
        # The requested day has to be inside the fetched window, whatever the
        # caller said about forecast_days.
        forecast_days = max(int(arguments.get("forecast_days", 1)), day + 1)
        units = str(arguments.get("units", self.config.units))
        language = str(arguments.get("language", self.config.language))

        polled = self._polled(location, forecast_days, units)
        if polled is not None:
            snapshot, payload = polled
            return {
                **render_forecast(payload, language, day=day),
                "cached": True,
                "source": "poll",
                "polled_at": snapshot.polled_at_utc,
            }
        cache_key = (location.casefold(), forecast_days, units)
        cached = self._cached(cache_key)
        if cached is not None:
            return {
                **render_forecast(cached, language, day=day),
                "cached": True,
                "source": "cache",
            }

        context.raise_if_cancelled()
        timeout = min(self.config.request_timeout_seconds, context.remaining_seconds())
        try:
            place = self._geocode(location, timeout)
            context.raise_if_cancelled()
            timeout = min(self.config.request_timeout_seconds, context.remaining_seconds())
            payload = self._forecast(place, forecast_days, units, timeout)
        except ToolRejected:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ToolRejected("weather service is temporarily unavailable") from error
        context.raise_if_cancelled()
        with self._lock:
            self._cache[cache_key] = (self.now(), dict(payload))
        return {
            **render_forecast(payload, language, day=day),
            "cached": False,
            "source": "live",
        }

    def weather_set_home(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        """Set the household location, after checking the place is real."""
        raw_location = arguments.get("location")
        if not isinstance(raw_location, str):
            raise ToolRejected("location is required")
        location = validate_location(raw_location)
        language = str(arguments.get("language", self.config.language))
        context.raise_if_cancelled()
        timeout = min(self.config.request_timeout_seconds, context.remaining_seconds())
        try:
            # Resolving first means a typo is refused now rather than becoming
            # a poller that quietly fails every fifteen minutes.
            place = self._geocode(location, timeout)
        except ToolRejected:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ToolRejected("weather service is temporarily unavailable") from error
        name = str(place["name"])
        self.home.set(name)
        if language == "es":
            summary = f"El tiempo de casa ahora es {name}."
        else:
            summary = f"Home weather is now set to {name}."
        return {
            "location": {
                "name": name,
                "admin1": place["admin1"],
                "country": place["country"],
                "timezone": place["timezone"],
            },
            "summary": summary,
        }

    def fetch(
        self, location: str, forecast_days: int, units: str, timeout: float
    ) -> dict[str, object]:
        """Fetch one language-neutral forecast, bypassing every cache."""
        place = self._geocode(location, timeout)
        return self._forecast(place, forecast_days, units, timeout)

    def _polled(
        self, location: str, forecast_days: int, units: str
    ) -> tuple[WeatherSnapshot, Mapping[str, object]] | None:
        if self.snapshots is None:
            return None
        current = self.snapshots.current()
        if current is None:
            return None
        snapshot, age = current
        if (
            snapshot.location_key != location.casefold()
            or snapshot.units != units
            or snapshot.forecast_days < forecast_days
            or age > self.config.poll_seconds * STALE_SNAPSHOT_MULTIPLIER
        ):
            return None
        payload = dict(snapshot.payload)
        forecast = list(payload["forecast"])  # type: ignore[arg-type]
        payload["forecast"] = forecast[:forecast_days]
        return snapshot, payload

    def _cached(self, key: tuple[str, int, str]) -> dict[str, object] | None:
        with self._lock:
            item = self._cache.get(key)
            if item is None:
                return None
            stored_at, value = item
            if self.now() - stored_at >= self.config.cache_seconds:
                self._cache.pop(key, None)
                return None
            return dict(value)

    def _geocode(self, location: str, timeout: float) -> dict[str, object]:
        query = urlencode(
            {
                "name": location,
                "count": 1,
                "language": GEOCODING_LANGUAGE,
                "format": "json",
            }
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
        current_result = {
            "observed_at": _text(current, "time"),
            "weather_code": int(_number(current, "weather_code")),
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
        for index, date in enumerate(dates[:forecast_days]):
            forecast.append(
                {
                    "date": str(date),
                    "weather_code": int(_finite_number(codes[index])),
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
            "attribution": ATTRIBUTION,
            "attribution_url": ATTRIBUTION_URL,
        }


class WeatherPoller:
    """Keep one household forecast warm so answers never wait on the network.

    The screen reads the same snapshot the voice answer does, so the panel and
    Miso can never disagree about whether it is going to rain.
    """

    def __init__(
        self,
        adapter: OpenMeteoWeatherAdapter,
        snapshots: WeatherSnapshotStore,
        *,
        home: WeatherHome,
        units: str = "metric",
        interval_seconds: float = DEFAULT_POLL_SECONDS,
        forecast_days: int = POLL_FORECAST_DAYS,
        on_update: Callable[[WeatherSnapshot], None] | None = None,
    ) -> None:
        if not MIN_POLL_SECONDS <= interval_seconds <= MAX_POLL_SECONDS:
            raise ValueError("weather poll interval must be between 60 and 86400 seconds")
        if not 1 <= forecast_days <= 7:
            raise ValueError("weather poll forecast days must be between 1 and 7")
        self.adapter = adapter
        self.snapshots = snapshots
        self.home = home
        self.units = units
        self.interval_seconds = interval_seconds
        self.forecast_days = forecast_days
        self.on_update = on_update
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="miso-weather-poll", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.adapter.config.request_timeout_seconds * 2))

    def poke(self) -> None:
        """Ask the running loop to refresh now, after a location change."""
        self._wake.set()

    def refresh_once(self) -> WeatherSnapshot | None:
        location = self.home.location()
        if not location:
            # Nothing is configured yet. The dashboard and the set-home tool
            # both wake the loop, so there is nothing to retry here.
            return None
        try:
            payload = self.adapter.fetch(
                location,
                self.forecast_days,
                self.units,
                self.adapter.config.request_timeout_seconds,
            )
        except ToolRejected as error:
            self.snapshots.record_failure(str(error))
            LOGGER.warning("weather poll rejected: %s", error)
            return None
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            self.snapshots.record_failure("weather service is temporarily unavailable")
            # The location is household data and stays out of the log line.
            LOGGER.warning("weather poll failed: %s", type(error).__name__)
            return None
        snapshot = self.snapshots.store(
            payload,
            location_key=location.casefold(),
            units=self.units,
            forecast_days=self.forecast_days,
        )
        if self.on_update is not None:
            try:
                self.on_update(snapshot)
            except Exception:
                # Publishing to the screen must never stop the next poll.
                LOGGER.exception("weather snapshot listener failed")
        return snapshot

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            self.refresh_once()
            # A poke lands as soon as someone changes the location, so a new
            # place is on the screen in a second rather than at the next poll.
            self._wake.wait(self.interval_seconds)
            if self._stop.is_set():
                return


def create_weather_poller(
    config: WeatherConfig,
    snapshots: WeatherSnapshotStore,
    *,
    home: WeatherHome | None = None,
    transport: WeatherTransport | None = None,
    on_update: Callable[[WeatherSnapshot], None] | None = None,
) -> WeatherPoller | None:
    """Build the household poller, or None when polling is switched off.

    The poller exists even with no location set yet: the household can set one
    from the dashboard later, and the poller picks it up on the next poke.
    """
    if not config.poll_seconds:
        return None
    resolved_home = home or WeatherHome(config.default_location)
    poller = WeatherPoller(
        OpenMeteoWeatherAdapter(config, transport=transport, home=resolved_home),
        snapshots,
        home=resolved_home,
        units=config.units,
        interval_seconds=config.poll_seconds,
        on_update=on_update,
    )
    # A new location makes the stored snapshot the wrong town, so it is dropped
    # rather than left on the screen until the refresh lands.
    resolved_home.add_listener(lambda _location: (snapshots.clear(), poller.poke()))
    return poller


def render_forecast(
    payload: Mapping[str, object], language: str, *, day: int = 0
) -> dict[str, object]:
    """Add the spoken summary and condition words to a neutral forecast.

    ``day`` picks which forecast entry the summary speaks about: 0 is today
    with live conditions, later days are described from the daily outlook.
    """
    current = dict(_mapping(payload, "current"))
    current["conditions"] = _condition(int(_number(current, "weather_code")), language)
    forecast = [
        {**dict(day), "conditions": _condition(int(_number(day, "weather_code")), language)}
        for day in payload["forecast"]  # type: ignore[union-attr]
        if isinstance(day, Mapping)
    ]
    location = _mapping(payload, "location")
    if day >= len(forecast):
        raise ToolRejected("forecast does not reach the requested day")
    if day == 0:
        summary = _summary(location, current, forecast[0], language)
    else:
        summary = _day_summary(location, forecast[day], day, language)
    return {
        **dict(payload),
        "current": current,
        "forecast": forecast,
        "day": day,
        "summary": summary,
    }


def weather_panel(snapshot: WeatherSnapshot, language: str) -> dict[str, object]:
    """The compact shape the companion screen and /api/status both render."""
    rendered = render_forecast(snapshot.payload, language)
    current = _mapping(rendered, "current")
    today = rendered["forecast"][0]  # type: ignore[index]
    raining_now = _number(current, "precipitation") > 0
    chance = _number(today, "precipitation_probability_max")
    return {
        "location": _mapping(rendered, "location")["name"],
        "conditions": current["conditions"],
        "weather_code": current["weather_code"],
        "temperature": _number(current, "temperature"),
        "temperature_unit": current["temperature_unit"],
        "temperature_max": _number(today, "temperature_max"),
        "temperature_min": _number(today, "temperature_min"),
        "rain_chance": chance,
        "raining_now": raining_now,
        "rain_text": _rain_label(today, raining_now, chance, language),
        "updated_at": snapshot.polled_at_utc,
        "attribution": ATTRIBUTION,
    }


def weather_status(
    snapshots: WeatherSnapshotStore, language: str = "en"
) -> dict[str, object]:
    """Weather for /api/status: the panel plus why it might be missing."""
    current = snapshots.current()
    failure = snapshots.failure()
    if current is None:
        return {"available": False, "error": failure}
    snapshot, age = current
    return {
        "available": True,
        "error": failure,
        "age_seconds": round(age, 1),
        **weather_panel(snapshot, language),
    }


def weather_tool_definition(adapter: OpenMeteoWeatherAdapter) -> ToolDefinition:
    properties: dict[str, object] = {
        "location": {
            "type": "string",
            "minLength": 1,
            "maxLength": 120,
            "description": "City or place name, such as London or Madrid",
        },
        "day": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_FORECAST_DAYS - 1,
            "description": "Which day to describe: 0 today, 1 tomorrow, 2 the day after",
        },
        "forecast_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_FORECAST_DAYS,
            "description": "Number of forecast days including today",
        },
        "units": {"type": "string", "enum": ["metric", "imperial"]},
        "language": {"type": "string", "enum": ["en", "es"]},
    }
    required = [] if adapter.home.location() else ["location"]
    return ToolDefinition(
        name="weather_get",
        description=(
            "Get the weather for a real location using Open-Meteo, day 0 today "
            "and day 1 tomorrow; consulta el tiempo y el pronóstico. Use "
            "language es for Spanish requests."
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


def weather_set_home_tool_definition(
    adapter: OpenMeteoWeatherAdapter,
) -> ToolDefinition:
    return ToolDefinition(
        name="weather_set_home",
        description=(
            "Set the household's home weather location, the place used when "
            "nobody names one; cambia la ubicación del tiempo de casa."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                    "description": "City or place name to use as home, such as Madrid",
                },
                "language": {"type": "string", "enum": ["en", "es"]},
            },
            "required": ["location"],
            "additionalProperties": False,
        },
        handler=adapter.weather_set_home,
        timeout_seconds=8,
        redact_fields=frozenset({"location"}),
    )


def register_weather_tools(
    registry: ToolRegistry,
    config: WeatherConfig,
    *,
    transport: WeatherTransport | None = None,
    now: Callable[[], float] = time.monotonic,
    snapshots: WeatherSnapshotStore | None = None,
    home: WeatherHome | None = None,
) -> OpenMeteoWeatherAdapter:
    adapter = OpenMeteoWeatherAdapter(
        config, transport=transport, now=now, snapshots=snapshots, home=home
    )
    registry.register(weather_tool_definition(adapter))
    registry.register(weather_set_home_tool_definition(adapter))

    # Whether `location` is required depends on whether a home is set, so the
    # schema is rebuilt when the household changes it.
    adapter.home.add_listener(
        lambda _location: registry.register(
            weather_tool_definition(adapter), replace=True
        )
    )
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
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})
# WMO codes whose condition word already says something is falling. When the
# probability sits below "possible" for one of these, "light drizzle and no
# rain expected" would contradict itself, so the rain clause is left out.
_PRECIPITATION_CODES = frozenset(
    {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
) | _SNOW_CODES
# Above this it is worth carrying an umbrella; below the lower bound Miso says
# the day is dry rather than reading out a number nobody acts on.
RAIN_LIKELY_PERCENT = 60.0
RAIN_POSSIBLE_PERCENT = 30.0


def _condition(code: int, language: str) -> str:
    descriptions = _CONDITIONS.get(code, ("unknown conditions", "condiciones desconocidas"))
    return descriptions[1 if language == "es" else 0]


def _format_number(value: object) -> str:
    number = _finite_number(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _is_snow(today: Mapping[str, object]) -> bool:
    return int(_number(today, "weather_code")) in _SNOW_CODES


def _rain_phrase(
    current: Mapping[str, object], today: Mapping[str, object], language: str
) -> str:
    snow = _is_snow(today) or int(_number(current, "weather_code")) in _SNOW_CODES
    if _number(current, "precipitation") > 0:
        if language == "es":
            return "Está nevando ahora" if snow else "Está lloviendo ahora"
        return "It is snowing right now" if snow else "It is raining right now"
    return _rain_outlook(today, "hoy" if language == "es" else "today", language, snow)


def _rain_outlook(
    day: Mapping[str, object], when: str, language: str, snow: bool | None = None
) -> str:
    """The chance-of-rain clause for one day, with ``when`` already localised."""
    if snow is None:
        snow = _is_snow(day)
    chance = _format_number(_number(day, "precipitation_probability_max"))
    percent = _number(day, "precipitation_probability_max")
    if (
        percent < RAIN_POSSIBLE_PERCENT
        and int(_number(day, "weather_code")) in _PRECIPITATION_CODES
    ):
        return ""
    if language == "es":
        precipitation = "Nieve" if snow else "Lluvia"
        if percent >= RAIN_LIKELY_PERCENT:
            return f"{precipitation} probable {when}, {chance}%"
        if percent >= RAIN_POSSIBLE_PERCENT:
            return f"{precipitation} posible {when}, {chance}%"
        return f"No se espera lluvia {when}"
    precipitation = "snow" if snow else "rain"
    if percent >= RAIN_LIKELY_PERCENT:
        return f"{precipitation} is likely {when}, {chance}% chance"
    if percent >= RAIN_POSSIBLE_PERCENT:
        return f"{precipitation} is possible {when}, {chance}% chance"
    return f"no rain expected {when}"


def _rain_label(
    today: Mapping[str, object], raining_now: bool, chance: float, language: str
) -> str:
    """The short rain line on the panel, where space is one line of text."""
    snow = _is_snow(today)
    if raining_now:
        if language == "es":
            return "Nevando ahora" if snow else "Lloviendo ahora"
        return "Snowing now" if snow else "Raining now"
    percent = _format_number(chance)
    if language == "es":
        return f"{'Nieve' if snow else 'Lluvia'} {percent}%"
    return f"{'Snow' if snow else 'Rain'} {percent}%"


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
    rain = _rain_phrase(current, today, language)
    if language == "es":
        return (
            f"En {name} hay {current['conditions']} a {temperature}{temperature_unit}. "
            + (f"{rain}. " if rain else "")
            + f"Máxima de {high}{temperature_unit} y mínima de "
            f"{low}{temperature_unit}."
        )
    return (
        f"In {name} it is {current['conditions']} at {temperature}{temperature_unit}"
        + (f", and {rain}" if rain else "")
        + f". High {high}{temperature_unit}, low {low}{temperature_unit}."
    )


_WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAYS_ES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")


def _day_word(day: Mapping[str, object], offset: int, language: str) -> str:
    """"tomorrow" or the weekday name, so the answer names the day asked about."""
    if offset == 1:
        return "mañana" if language == "es" else "tomorrow"
    try:
        weekday = date.fromisoformat(str(day["date"])).weekday()
    except (KeyError, ValueError):
        return f"en {offset} días" if language == "es" else f"in {offset} days"
    if language == "es":
        return f"el {_WEEKDAYS_ES[weekday]}"
    return f"on {_WEEKDAYS_EN[weekday]}"


def _day_summary(
    place: Mapping[str, object],
    day: Mapping[str, object],
    offset: int,
    language: str,
) -> str:
    """Speak one future day: no live temperature exists for it, so lead with
    the expected conditions and the high and low."""
    name = str(place["name"])
    unit = str(day["temperature_unit"])
    high = _format_number(day["temperature_max"])
    low = _format_number(day["temperature_min"])
    when = _day_word(day, offset, language)
    rain = _rain_outlook(day, when, language)
    if language == "es":
        return (
            f"En {name} {when} se espera {day['conditions']}, con máxima de "
            f"{high}{unit} y mínima de {low}{unit}." + (f" {rain}." if rain else "")
        )
    return (
        f"In {name} {when} expect {day['conditions']}, high {high}{unit}, "
        f"low {low}{unit}" + (f", and {rain}." if rain else ".")
    )
