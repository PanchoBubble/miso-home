import json
from urllib.parse import parse_qs, urlsplit
import unittest

from miso.tools import (
    InMemoryAuditLog,
    ToolRegistry,
    ToolStatus,
    WeatherConfig,
    WeatherHome,
    WeatherPoller,
    WeatherSnapshotStore,
    create_weather_poller,
    register_weather_tools,
    weather_status,
)


class FakeWeatherTransport:
    def __init__(
        self,
        *,
        weather_code: int = 2,
        precipitation: float = 0.0,
        rain_chance: float = 20.0,
    ) -> None:
        self.calls = []
        self.weather_code = weather_code
        self.precipitation = precipitation
        self.rain_chance = rain_chance
        self.fail = False

    def __call__(self, url: str, timeout_seconds: float) -> bytes:
        self.calls.append((url, timeout_seconds))
        if self.fail:
            raise TimeoutError("weather transport is down")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.hostname == "geocoding-api.open-meteo.com":
            # Echo the place that was asked for, so a change of home is
            # visible in the resolved name the way the real API behaves.
            return json.dumps(
                {
                    "results": [
                        {
                            "name": query["name"][0],
                            "admin1": "England",
                            "country": "United Kingdom",
                            "latitude": 51.5085,
                            "longitude": -0.1257,
                            "timezone": "Europe/London",
                        }
                    ]
                }
            ).encode()
        days = int(query["forecast_days"][0])
        return json.dumps(
            {
                "timezone": "Europe/London",
                "current": {
                    "time": "2026-08-29T10:00",
                    "temperature_2m": 18.4,
                    "apparent_temperature": 17.8,
                    "weather_code": self.weather_code,
                    "precipitation": self.precipitation,
                    "wind_speed_10m": 12.5,
                },
                "current_units": {
                    "temperature_2m": "°C",
                    "apparent_temperature": "°C",
                    "precipitation": "mm",
                    "wind_speed_10m": "km/h",
                },
                "daily": {
                    "time": [f"2026-08-{29 + index:02d}" for index in range(days)],
                    "weather_code": [self.weather_code] * days,
                    "temperature_2m_max": [21.0] * days,
                    "temperature_2m_min": [13.0] * days,
                    "precipitation_probability_max": [self.rain_chance] * days,
                },
                "daily_units": {
                    "temperature_2m_max": "°C",
                    "temperature_2m_min": "°C",
                    "precipitation_probability_max": "%",
                },
            }
        ).encode()


class WeatherToolTests(unittest.TestCase):
    def registry(self, config=None, transport=None, now=None, snapshots=None):
        self.audit = InMemoryAuditLog()
        registry = ToolRegistry(self.audit)
        register_weather_tools(
            registry,
            config or WeatherConfig(),
            transport=transport,
            snapshots=snapshots,
            **({"now": now} if now is not None else {}),
        )
        return registry

    def test_fetches_compact_forecast_caches_and_redacts_location(self) -> None:
        transport = FakeWeatherTransport()
        clock = [100.0]
        registry = self.registry(transport=transport, now=lambda: clock[0])

        first = registry.invoke(
            "weather_get",
            {
                "location": "London",
                "forecast_days": 2,
                "units": "metric",
                "language": "en",
            },
        )
        self.assertEqual(first.status, ToolStatus.SUCCESS, first.error)
        self.assertEqual(first.output["location"]["name"], "London")
        self.assertEqual(len(first.output["forecast"]), 2)
        self.assertIn("partly cloudy", first.summary)
        self.assertIn("no rain expected today", first.summary)
        # The spoken answer is the weather, not the paperwork: attribution
        # stays in the structured output for the dashboard.
        self.assertNotIn("Open-Meteo", first.summary)
        self.assertEqual(first.output["attribution"], "Weather data by Open-Meteo.com")
        self.assertFalse(first.output["cached"])
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(all(call[1] <= 6 for call in transport.calls))
        geocoding_query = parse_qs(urlsplit(transport.calls[0][0]).query)
        self.assertEqual(geocoding_query["name"], ["London"])

        second = registry.invoke(
            "weather_get",
            {
                "location": "london",
                "forecast_days": 2,
                "units": "metric",
                "language": "en",
            },
        )
        self.assertTrue(second.output["cached"])
        self.assertEqual(len(transport.calls), 2)
        started = next(
            event
            for event in self.audit.events()
            if event["event"] == "tool_invocation_started"
        )
        self.assertEqual(started["arguments"]["location"], "[REDACTED]")

    def test_default_location_is_optional_and_spanish_summary_is_supported(self) -> None:
        transport = FakeWeatherTransport()
        registry = self.registry(
            WeatherConfig(default_location="London"), transport=transport
        )
        schema = registry.get("weather_get").input_schema
        self.assertEqual(schema["required"], [])
        result = registry.invoke("weather_get", {"language": "es"})
        self.assertEqual(result.status, ToolStatus.SUCCESS, result.error)
        self.assertIn("En London", result.summary)
        self.assertIn("cielo parcialmente nublado", result.summary)
        self.assertIn("No se espera lluvia hoy", result.summary)

    def test_location_is_required_without_home_default(self) -> None:
        registry = self.registry(transport=FakeWeatherTransport())
        self.assertEqual(registry.get("weather_get").input_schema["required"], ["location"])
        result = registry.invoke("weather_get", {})
        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertIn("missing required property location", result.error)

    def test_configuration_rejects_non_finite_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "request timeout"):
            WeatherConfig(request_timeout_seconds=float("nan"))
        with self.assertRaisesRegex(ValueError, "weather cache"):
            WeatherConfig(cache_seconds=float("inf"))

    def test_zero_cache_duration_disables_caching(self) -> None:
        transport = FakeWeatherTransport()
        registry = self.registry(
            WeatherConfig(cache_seconds=0), transport=transport, now=lambda: 100.0
        )
        arguments = {"location": "London"}
        self.assertFalse(registry.invoke("weather_get", arguments).output["cached"])
        self.assertFalse(registry.invoke("weather_get", arguments).output["cached"])
        self.assertEqual(len(transport.calls), 4)

    def test_missing_location_and_invalid_responses_are_bounded(self) -> None:
        def missing(_url, _timeout):
            return b'{"results":[]}'

        missing_result = self.registry(transport=missing).invoke(
            "weather_get", {"location": "Atlantis"}
        )
        self.assertEqual(missing_result.status, ToolStatus.REJECTED)
        self.assertEqual(missing_result.error, "weather location was not found")

        def malformed(_url, _timeout):
            return b"not-json"

        malformed_result = self.registry(transport=malformed).invoke(
            "weather_get", {"location": "London"}
        )
        self.assertEqual(malformed_result.status, ToolStatus.REJECTED)
        self.assertEqual(
            malformed_result.error, "weather service is temporarily unavailable"
        )


    def test_rain_is_reported_when_it_is_likely_and_while_it_falls(self) -> None:
        transport = FakeWeatherTransport(weather_code=61, precipitation=0.0, rain_chance=80.0)
        likely = self.registry(transport=transport).invoke(
            "weather_get", {"location": "London"}
        )
        self.assertIn("rain is likely today, 80% chance", likely.summary)

        falling = self.registry(
            transport=FakeWeatherTransport(weather_code=63, precipitation=0.6)
        ).invoke("weather_get", {"location": "London"})
        self.assertIn("It is raining right now", falling.summary)

        snowing = self.registry(
            transport=FakeWeatherTransport(weather_code=73, rain_chance=90.0)
        ).invoke("weather_get", {"location": "Oslo", "language": "es"})
        self.assertIn("Nieve probable hoy, 90%", snowing.summary)


class WeatherPollingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [1000.0]
        self.transport = FakeWeatherTransport()
        self.snapshots = WeatherSnapshotStore(now=lambda: self.clock[0])
        self.config = WeatherConfig(default_location="London", poll_seconds=900)
        self.home = WeatherHome(self.config.default_location)
        self.registry = ToolRegistry(InMemoryAuditLog())
        register_weather_tools(
            self.registry,
            self.config,
            transport=self.transport,
            now=lambda: self.clock[0],
            snapshots=self.snapshots,
            home=self.home,
        )
        self.poller = create_weather_poller(
            self.config, self.snapshots, home=self.home, transport=self.transport
        )

    def test_polled_snapshot_answers_without_touching_the_network(self) -> None:
        self.assertIsNotNone(self.poller.refresh_once())
        polled_calls = len(self.transport.calls)

        result = self.registry.invoke("weather_get", {"language": "es"})
        self.assertEqual(result.status, ToolStatus.SUCCESS, result.error)
        self.assertEqual(result.output["source"], "poll")
        self.assertTrue(result.output["cached"])
        self.assertIn("En London", result.summary)
        self.assertEqual(len(self.transport.calls), polled_calls)

    def test_another_location_and_a_longer_forecast_still_go_live(self) -> None:
        self.poller.refresh_once()
        polled_calls = len(self.transport.calls)

        away = self.registry.invoke("weather_get", {"location": "Madrid"})
        self.assertEqual(away.output["source"], "live")
        self.assertGreater(len(self.transport.calls), polled_calls)

        week = self.registry.invoke("weather_get", {"forecast_days": 7})
        self.assertEqual(week.output["source"], "live")
        self.assertEqual(len(week.output["forecast"]), 7)

    def test_a_stale_snapshot_is_replaced_by_a_live_lookup(self) -> None:
        self.poller.refresh_once()
        self.clock[0] += 900 * 3 + 1
        result = self.registry.invoke("weather_get", {})
        self.assertEqual(result.output["source"], "live")

    def test_a_failed_poll_keeps_the_last_snapshot_and_records_why(self) -> None:
        snapshot = self.poller.refresh_once()
        self.transport.fail = True
        self.assertIsNone(self.poller.refresh_once())
        self.assertEqual(
            self.snapshots.failure(), "weather service is temporarily unavailable"
        )
        current = self.snapshots.current()
        self.assertIsNotNone(current)
        self.assertEqual(current[0].polled_at_utc, snapshot.polled_at_utc)

    def test_panel_payload_carries_what_the_screen_draws(self) -> None:
        updates = []
        poller = WeatherPoller(
            self.poller.adapter,
            self.snapshots,
            home=self.home,
            interval_seconds=900,
            on_update=updates.append,
        )
        poller.refresh_once()
        self.assertEqual(len(updates), 1)

        panel = weather_status(self.snapshots, "en")
        self.assertTrue(panel["available"])
        self.assertEqual(panel["location"], "London")
        self.assertEqual(panel["weather_code"], 2)
        self.assertEqual(panel["temperature_unit"], "°C")
        self.assertEqual(panel["rain_text"], "Rain 20%")
        self.assertFalse(panel["raining_now"])
        self.assertIn("attribution", panel)

    def test_status_reports_nothing_before_the_first_poll(self) -> None:
        status = weather_status(WeatherSnapshotStore(), "en")
        self.assertFalse(status["available"])
        self.assertIsNone(status["error"])

    def test_polling_waits_for_a_location_and_a_zero_interval_disables_it(self) -> None:
        # No location yet: the poller exists and idles, because the household
        # can set one from the dashboard without a restart.
        waiting = create_weather_poller(
            WeatherConfig(), self.snapshots, transport=self.transport
        )
        self.assertIsNotNone(waiting)
        self.assertIsNone(waiting.refresh_once())
        self.assertIsNone(self.snapshots.current())

        self.assertIsNone(
            create_weather_poller(
                WeatherConfig(default_location="London", poll_seconds=0),
                WeatherSnapshotStore(),
            )
        )
        with self.assertRaisesRegex(ValueError, "poll interval"):
            WeatherConfig(default_location="London", poll_seconds=5)

    def test_setting_the_home_repolls_and_moves_the_answer(self) -> None:
        self.poller.refresh_once()
        self.assertEqual(self.snapshots.current()[0].location_key, "london")

        moved = self.registry.invoke("weather_set_home", {"location": "Madrid"})
        self.assertEqual(moved.status, ToolStatus.SUCCESS, moved.error)
        self.assertEqual(moved.output["location"]["name"], "Madrid")
        self.assertIn("Madrid", moved.summary)
        self.assertEqual(self.home.location(), "Madrid")
        # The old town's snapshot is dropped rather than left on the screen.
        self.assertIsNone(self.snapshots.current())

        self.poller.refresh_once()
        self.assertEqual(self.snapshots.current()[0].location_key, "madrid")
        answered = self.registry.invoke("weather_get", {})
        self.assertEqual(answered.output["source"], "poll")
        self.assertIn("Madrid", answered.summary)

    def test_home_can_be_set_later_and_the_schema_follows(self) -> None:
        registry = ToolRegistry(InMemoryAuditLog())
        home = WeatherHome()
        register_weather_tools(
            registry, WeatherConfig(), transport=self.transport, home=home
        )
        self.assertEqual(registry.get("weather_get").input_schema["required"], ["location"])

        home.set("London")
        self.assertEqual(registry.get("weather_get").input_schema["required"], [])
        home.set(None)
        self.assertEqual(registry.get("weather_get").input_schema["required"], ["location"])

    def test_an_unknown_place_is_refused_before_it_is_stored(self) -> None:
        def missing(_url, _timeout):
            return b'{"results":[]}'

        registry = ToolRegistry(InMemoryAuditLog())
        home = WeatherHome("London")
        register_weather_tools(registry, self.config, transport=missing, home=home)
        result = registry.invoke("weather_set_home", {"location": "Atlantis"})
        self.assertEqual(result.status, ToolStatus.REJECTED)
        self.assertEqual(result.error, "weather location was not found")
        self.assertEqual(home.location(), "London")

    def test_the_home_is_persisted_through_its_own_hook(self) -> None:
        written = []
        home = WeatherHome("London", persist=written.append)
        home.set("Madrid")
        home.set("Madrid")
        home.set(None)
        self.assertEqual(written, ["Madrid", None])


if __name__ == "__main__":
    unittest.main()
