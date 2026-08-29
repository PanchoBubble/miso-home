import json
from urllib.parse import parse_qs, urlsplit
import unittest

from miso.tools import (
    InMemoryAuditLog,
    ToolRegistry,
    ToolStatus,
    WeatherConfig,
    register_weather_tools,
)


class FakeWeatherTransport:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, url: str, timeout_seconds: float) -> bytes:
        self.calls.append((url, timeout_seconds))
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.hostname == "geocoding-api.open-meteo.com":
            return json.dumps(
                {
                    "results": [
                        {
                            "name": "London",
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
                    "weather_code": 2,
                    "precipitation": 0.0,
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
                    "weather_code": [2] * days,
                    "temperature_2m_max": [21.0] * days,
                    "temperature_2m_min": [13.0] * days,
                    "precipitation_probability_max": [20.0] * days,
                },
                "daily_units": {
                    "temperature_2m_max": "°C",
                    "temperature_2m_min": "°C",
                    "precipitation_probability_max": "%",
                },
            }
        ).encode()


class WeatherToolTests(unittest.TestCase):
    def registry(self, config=None, transport=None, now=None):
        self.audit = InMemoryAuditLog()
        registry = ToolRegistry(self.audit)
        register_weather_tools(
            registry,
            config or WeatherConfig(),
            transport=transport,
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
        self.assertIn("Open-Meteo.com", first.summary)
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


if __name__ == "__main__":
    unittest.main()
