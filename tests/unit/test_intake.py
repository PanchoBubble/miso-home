from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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

    def test_half_hour_and_two_more_minutes(self) -> None:
        reply = self.lane.try_handle("Set a timer for half an hour", "en")
        self.assertEqual(reply.spoken, "Timer set for 30 minutes.")
        self.assertEqual(match_fast_intent("Add two more minutes", "en"),
                         ("timer_control", {"action": "extend", "seconds": 120}))

    def test_timer_without_a_duration_falls_through(self) -> None:
        self.assertIsNone(self.lane.try_handle("Set a timer", "en"))

    def test_unknown_named_timer_is_not_cancelled_by_guessing(self) -> None:
        self.lane.try_handle("Set a timer for 5 minutes", "en")
        reply = self.lane.try_handle("Cancel the 5 minute timer", "en")
        self.assertEqual(reply.result.output["outcome"], "not_found")
        self.assertEqual(len(self.registry.invoke("timer_list", {}).output["timers"]), 1)

    def test_named_timer_conversation(self) -> None:
        def say(text):
            return self.lane.try_handle(text, "en", conversation_id="kitchen")
        created = say("Set a pasta timer for ten minutes")
        self.assertIn("pasta timer", created.spoken)
        original_due = created.result.output["timer"]["due_at"]
        extended = say("Add two minutes")
        from datetime import datetime
        delta = datetime.fromisoformat(extended.result.output["timer"]["due_at"]) - datetime.fromisoformat(original_due)
        self.assertEqual(delta.total_seconds(), 120)
        self.assertIn("pasta", say("How long left?").spoken)
        self.assertEqual(say("Cancel it").result.output["timer"]["status"], "cancelled")

    def test_timer_clarification_is_scoped_to_conversation(self) -> None:
        self.lane.try_handle("Set a pasta timer for ten minutes", "en")
        self.lane.try_handle("Set a tea timer for five minutes", "en")
        reply = self.lane.try_handle("Add two minutes", "en", conversation_id="one")
        self.assertEqual(reply.result.output["outcome"], "ambiguous")
        self.assertIsNone(self.lane.try_handle("pasta", "en", conversation_id="two"))
        reply = self.lane.try_handle("pasta", "en", conversation_id="one")
        self.assertEqual(reply.result.output["timer"]["title"], "pasta")

    def test_shopping_followups(self) -> None:
        def say(text):
            return self.lane.try_handle(text, "en", conversation_id="groceries")
        say("Add milk and two eggs to the shopping list")
        self.assertEqual(say("And bread").spoken, "Added bread.")
        self.assertEqual(say("Remove milk").spoken, "Removed milk.")
        self.assertIn("bread", say("Read the list").spoken)
        self.assertIsNone(self.lane.try_handle("And coffee", "en", conversation_id="unrelated"))

    def test_ambiguous_shopping_removal_does_not_guess(self) -> None:
        self.lane.try_handle("Add oat milk and almond milk to the list", "en")
        reply = self.lane.try_handle("Remove milk from the list", "en", conversation_id="shop")
        self.assertEqual(reply.result.output["outcome"], "ambiguous")
        self.assertEqual(len(self.registry.invoke("shopping_list", {}).output["items"]), 2)
        reply = self.lane.try_handle("oat milk", "en", conversation_id="shop")
        self.assertEqual(reply.spoken, "Removed oat milk.")

    def test_timer_modification_never_creates_a_timer(self) -> None:
        for phrase in ("Add two minutes to the pasta timer", "Cancel the pasta timer",
                       "How long left on the pasta timer"):
            self.assertEqual(match_fast_intent(phrase, "en")[0], "timer_control")
        for phrase in ("Don't set a timer for five minutes", "Set a timer for 1.5 minutes",
                       "If I set a timer for ten minutes", "Set a timer for five minutes tomorrow"):
            self.assertIsNone(match_fast_intent(phrase, "en"), phrase)

    def test_spanish_named_timer(self) -> None:
        self.lane.try_handle("Pon un temporizador de diez minutos para pasta", "es")
        reply = self.lane.try_handle("Añade dos minutos al temporizador de pasta", "es")
        self.assertEqual(reply.result.output["timer"]["title"], "pasta")
        reply = self.lane.try_handle("Cancela el temporizador de pasta", "es")
        self.assertEqual(reply.result.output["timer"]["status"], "cancelled")

    def test_shopping_round_trip(self) -> None:
        added = self.lane.try_handle("Add milk to the shopping list", "en")
        self.assertIsNotNone(added)
        self.assertEqual(added.tool, "shopping_add")
        self.assertEqual(added.spoken, "Added milk.")
        listed = self.lane.try_handle("What's on the shopping list", "en")
        self.assertIsNotNone(listed)
        self.assertEqual(listed.tool, "shopping_list")
        self.assertEqual(listed.spoken, "On the list: milk.")

    def test_shopping_add_reads_a_leading_quantity(self) -> None:
        reply = self.lane.try_handle("Add 3 apples to the grocery list", "en")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.result.output["item"]["quantity"], 3)
        self.assertEqual(reply.result.output["item"]["name"], "apples")
        self.assertEqual(reply.spoken, "Added 3 apples.")

    def test_shopping_add_splits_several_items_in_one_utterance(self) -> None:
        reply = self.lane.try_handle("Add milk and eggs to the shopping list", "en")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.tool, "shopping_add_many")
        self.assertEqual(
            [item["name"] for item in reply.result.output["items"]],
            ["milk", "eggs"],
        )
        self.assertEqual(reply.spoken, "Added milk and eggs.")
        spanish = self.lane.try_handle(
            "Añade leche, pan y 2 huevos a la lista de la compra", "es"
        )
        self.assertIsNotNone(spanish)
        self.assertEqual(
            [item["name"] for item in spanish.result.output["items"]],
            ["leche", "pan", "huevos"],
        )
        self.assertEqual(spanish.result.output["items"][2]["quantity"], 2)
        self.assertEqual(spanish.spoken, "He añadido leche, pan y 2 huevos.")

    def test_shopping_remove_by_name_in_both_languages(self) -> None:
        self.lane.try_handle("Add milk to the shopping list", "en")
        self.lane.try_handle("Añade pan a la lista de la compra", "es")
        english = self.lane.try_handle("Remove milk from the shopping list", "en")
        self.assertIsNotNone(english)
        self.assertEqual(english.tool, "shopping_remove")
        self.assertEqual(english.spoken, "Removed milk.")
        spanish = self.lane.try_handle("Quita el pan de la lista", "es")
        self.assertIsNotNone(spanish)
        self.assertEqual(spanish.spoken, "He quitado pan.")
        listed = self.lane.try_handle("What's on the shopping list", "en")
        self.assertEqual(listed.spoken, "The shopping list is empty.")

    def test_shopping_remove_answers_an_item_that_is_not_listed(self) -> None:
        reply = self.lane.try_handle("Take chorizo off the shopping list", "en")
        self.assertIsNotNone(reply)
        self.assertEqual(reply.spoken, "Chorizo isn't on the list.")
        spanish = self.lane.try_handle("Quita el chorizo de la compra", "es")
        self.assertIsNotNone(spanish)
        self.assertEqual(spanish.spoken, "Chorizo no está en la lista.")

    def test_shopping_phrasings_reach_the_fast_lane(self) -> None:
        adds = (
            "put bread on the list",
            "stick coffee on the groceries list",
            "anade leche a la lista de la compra",
            "agrega dos leches a la lista",
            "mete el pan en la compra",
        )
        for phrase in adds:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    match_fast_intent(phrase, guess_language(phrase))[0],
                    "shopping_add",
                )
        removes = (
            "delete bread from the list",
            "cross eggs off the shopping list",
            "borra el pan de la lista",
            "saca la leche del super",
        )
        for phrase in removes:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    match_fast_intent(phrase, guess_language(phrase))[0],
                    "shopping_remove",
                )
        reads = (
            "what is in the list",
            "show the grocery list",
            "leeme la lista de la compra",
            "que falta en la lista",
        )
        for phrase in reads:
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    match_fast_intent(phrase, guess_language(phrase))[0],
                    "shopping_list",
                )

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


if __name__ == "__main__":
    unittest.main()
