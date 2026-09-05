from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from miso import intake
from miso.intake import FastLane, guess_language, match_fast_intent
from miso.tools import (
    InMemoryAuditLog,
    ToolDirectoryLoader,
    ToolRegistry,
    register_household_tools,
)


PORCH_MODULE = """
from miso.tools import ToolDefinition


def tool_definitions():
    return [
        ToolDefinition(
            "porch_light",
            "Switch the porch light",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda arguments, context: {"summary": "porch light switched"},
        )
    ]
"""


class FastLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.audit = InMemoryAuditLog()
        self.registry = ToolRegistry(self.audit)
        register_household_tools(
            self.registry, Path(self.temporary.name) / "miso.sqlite3"
        )
        self.lane = FastLane(self.registry, self.audit)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_timer_request_is_answered_without_a_model(self) -> None:
        reply = self.lane.try_handle("Set a timer for 5 minutes", "en")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.tool, "timer_create")
        self.assertTrue(reply.result.ok)
        self.assertEqual(reply.spoken, "Timer set for 5 minutes.")

    def test_spanish_timer_with_number_words(self) -> None:
        reply = self.lane.try_handle(
            "Pon un temporizador de diez minutos", "es"
        )
        self.assertIsNotNone(reply)
        self.assertEqual(reply.spoken, "Temporizador de 10 minutos en marcha.")

    def test_compound_duration_is_summed(self) -> None:
        reply = self.lane.try_handle(
            "set a timer for 1 hour 30 minutes", "en"
        )
        self.assertIsNotNone(reply)
        self.assertEqual(reply.spoken, "Timer set for 1 hour and 30 minutes.")

    def test_timer_without_a_duration_falls_through(self) -> None:
        self.assertIsNone(self.lane.try_handle("Set a timer", "en"))

    def test_timer_cancel_phrasing_falls_through(self) -> None:
        self.assertIsNone(
            self.lane.try_handle("Cancel the 5 minute timer", "en")
        )

    def test_shopping_round_trip(self) -> None:
        added = self.lane.try_handle("Add milk to the shopping list", "en")
        self.assertIsNotNone(added)
        self.assertEqual(added.tool, "shopping_add")
        self.assertEqual(added.spoken, "Added milk.")
        listed = self.lane.try_handle("What's on the shopping list", "en")
        self.assertIsNotNone(listed)
        self.assertEqual(listed.tool, "shopping_list")
        self.assertEqual(listed.spoken, "On the list: milk.")

    def test_spanish_shopping_add(self) -> None:
        reply = self.lane.try_handle(
            "Añade leche a la lista de la compra", "es"
        )
        self.assertIsNotNone(reply)
        self.assertEqual(reply.spoken, "He añadido leche.")

    def test_timer_list_reports_remaining_time(self) -> None:
        self.lane.try_handle("Set a timer for 10 minutes", "en")
        reply = self.lane.try_handle("How long is left on the timer", "en")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.tool, "timer_list")
        self.assertIn("left", reply.spoken)

    def test_unrelated_text_falls_through(self) -> None:
        self.assertIsNone(self.lane.try_handle("Tell me a joke", "en"))
        self.assertIsNone(
            self.lane.try_handle("Analyze my shopping habits over time", "en")
        )

    def test_unregistered_tool_falls_through(self) -> None:
        lane = FastLane(ToolRegistry(self.audit), self.audit)
        self.assertIsNone(lane.try_handle("Set a timer for 5 minutes", "en"))

    def test_disabled_lane_never_matches(self) -> None:
        lane = FastLane(self.registry, self.audit, enabled=False)
        self.assertIsNone(lane.try_handle("Set a timer for 5 minutes", "en"))

    def test_matches_are_audited(self) -> None:
        self.lane.try_handle("Set a timer for 5 minutes", "en")
        events = [
            event for event in self.audit.events() if event["event"] == "fast_intent"
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["intent"], "timer_create")
        self.assertEqual(events[0]["status"], "success")

    def test_refresh_tools_request_reloads_without_a_model(self) -> None:
        directory = Path(self.temporary.name) / "tools.d"
        directory.mkdir()
        loader = ToolDirectoryLoader(self.registry, directory, audit_sink=self.audit)
        self.registry.register(loader.tool_definition())
        (directory / "porch.py").write_text(PORCH_MODULE)

        reply = self.lane.try_handle("Refresh your tools", "en")

        self.assertIsNotNone(reply)
        self.assertEqual(reply.tool, "tools_refresh")
        self.assertEqual(reply.spoken, "Tools reloaded: 1 added.")
        self.assertIn("porch_light", self.registry.names())

        spanish = self.lane.try_handle("Recarga las herramientas", "es")
        self.assertIsNotNone(spanish)
        self.assertEqual(spanish.spoken, "Herramientas recargadas, sin cambios.")

    def test_refresh_tools_request_speaks_rejected_modules(self) -> None:
        directory = Path(self.temporary.name) / "tools.d"
        directory.mkdir()
        loader = ToolDirectoryLoader(self.registry, directory, audit_sink=self.audit)
        self.registry.register(loader.tool_definition())
        (directory / "broken.py").write_text(
            "def tool_definitions():\n    raise RuntimeError('boom')\n"
        )

        reply = self.lane.try_handle("reload tools", "en")

        self.assertIsNotNone(reply)
        self.assertEqual(
            reply.spoken,
            "Tools reloaded, nothing changed. I rejected these modules: broken.",
        )

    def test_refresh_phrasing_without_the_tool_falls_through(self) -> None:
        self.assertIsNone(self.lane.try_handle("Refresh your tools", "en"))
        self.assertIsNone(self.lane.try_handle("refresh the kitchen tools", "en"))

    def test_dry_run_match_reports_intent_without_running_a_tool(self) -> None:
        self.assertEqual(
            match_fast_intent("pon un temporizador de cinco segundos", "es"),
            ("timer_create", {"duration_seconds": 5}),
        )
        self.assertIsNone(match_fast_intent("Cinco Ceundas", "es"))
        self.assertIsNone(match_fast_intent("   ", "es"))

    def test_language_guess_for_typed_text(self) -> None:
        self.assertEqual(guess_language("¿Qué tiempo hace?"), "es")
        self.assertEqual(guess_language("pon un temporizador de 5 minutos"), "es")
        self.assertEqual(guess_language("what's the weather like"), "en")


class WeatherIntentTests(unittest.TestCase):
    """Which day a weather question names decides which forecast entry is spoken."""

    def setUp(self) -> None:
        self._today = intake._today
        intake._today = lambda: date(2026, 9, 5)  # a Saturday

    def tearDown(self) -> None:
        intake._today = self._today

    def test_today_has_no_day_argument(self) -> None:
        self.assertEqual(
            match_fast_intent("what's the weather like in London", "en"),
            ("weather_get", {"language": "en", "location": "london"}),
        )

    def test_tomorrow_in_either_language_and_word_order(self) -> None:
        for text, language, location in (
            ("what's the weather tomorrow", "en", None),
            ("weather in London tomorrow", "en", "london"),
            ("weather tomorrow in London", "en", "london"),
            ("qué tiempo hará mañana en Madrid", "es", "madrid"),
            ("el tiempo de mañana", "es", None),
        ):
            with self.subTest(text=text):
                expected = {"language": language, "day": 1}
                if location:
                    expected["location"] = location
                self.assertEqual(
                    match_fast_intent(text, language), ("weather_get", expected)
                )

    def test_the_morning_is_not_tomorrow(self) -> None:
        self.assertEqual(
            match_fast_intent("va a llover por la mañana", "es"),
            ("weather_get", {"language": "es"}),
        )
        self.assertEqual(
            match_fast_intent("is it going to rain in the morning", "en"),
            ("weather_get", {"language": "en"}),
        )

    def test_day_after_tomorrow_and_weekdays_count_from_today(self) -> None:
        self.assertEqual(
            match_fast_intent("weather the day after tomorrow", "en")[1]["day"], 2
        )
        self.assertEqual(
            match_fast_intent("pasado mañana va a llover", "es")[1]["day"], 2
        )
        self.assertEqual(
            match_fast_intent("will it rain on thursday", "en")[1]["day"], 5
        )
        self.assertEqual(
            match_fast_intent("qué tiempo hará el lunes en Bilbao", "es"),
            ("weather_get", {"language": "es", "day": 2, "location": "bilbao"}),
        )
        # Asking about today's weekday is asking about today.
        self.assertNotIn("day", match_fast_intent("weather this saturday", "en")[1])


if __name__ == "__main__":
    unittest.main()
