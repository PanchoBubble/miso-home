import unittest

from miso.buttons import STOP, TALK, ButtonManager, ButtonRouter
from miso.tools import InMemoryAuditLog
from miso.wake import WAKE_SOURCE_BUTTON, WakeEvent


class FakeWake:
    enabled = True

    def __init__(self) -> None:
        self.events: list[WakeEvent] = []

    def activate(self, event: WakeEvent) -> None:
        self.events.append(event)


class FakeConversation:
    def __init__(self, active: bool = True) -> None:
        self.active = active
        self.interrupts: list[str] = []

    def interrupt(self, reason: str) -> bool:
        self.interrupts.append(reason)
        return self.active


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(
    conversation: FakeConversation | None = None, **overrides: object
) -> tuple[ButtonRouter, FakeWake, FakeConversation, InMemoryAuditLog, FakeClock]:
    wake = FakeWake()
    talk_to = conversation or FakeConversation()
    audit = InMemoryAuditLog()
    clock = FakeClock()
    settings: dict[str, object] = {
        "wake": wake,
        "conversation": talk_to,
        "audit_sink": audit,
        "wake_phrase": "Miso",
        "debounce_seconds": 0.05,
        "hold_seconds": 1.0,
        "clock": clock,
    }
    settings.update(overrides)
    return ButtonRouter(**settings), wake, talk_to, audit, clock


class ButtonRouterTests(unittest.TestCase):
    def test_talk_press_publishes_a_button_sourced_wake_event(self) -> None:
        router, wake, _conversation, _audit, _clock = build()

        self.assertTrue(router.press(TALK))

        self.assertEqual(len(wake.events), 1)
        event = wake.events[0]
        self.assertEqual(event.phrase, "Miso")
        self.assertEqual(event.source, WAKE_SOURCE_BUTTON)
        self.assertEqual(event.score, 1.0)

    def test_talk_press_is_audited_with_the_button_actor_source(self) -> None:
        router, _wake, _conversation, audit, _clock = build()

        router.press(TALK)

        events = audit.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "button_press")
        self.assertEqual(events[0]["button"], TALK)
        self.assertEqual(events[0]["actor_source"], "button")

    def test_stop_press_interrupts_the_active_turn(self) -> None:
        router, wake, conversation, audit, _clock = build()

        self.assertTrue(router.press(STOP))

        self.assertEqual(conversation.interrupts, ["button stop"])
        self.assertEqual(wake.events, [])
        recorded = audit.events()[0]
        self.assertEqual(recorded["button"], STOP)
        self.assertEqual(recorded["actor_source"], "button")
        self.assertIs(recorded["interrupted"], True)

    def test_stop_press_with_nothing_in_flight_is_audited_as_a_no_op(self) -> None:
        router, _wake, _conversation, audit, _clock = build(FakeConversation(False))

        router.press(STOP)

        self.assertIs(audit.events()[0]["interrupted"], False)

    def test_contact_bounce_does_not_open_a_second_turn(self) -> None:
        router, wake, _conversation, _audit, clock = build()

        self.assertTrue(router.press(TALK))
        clock.advance(0.01)
        self.assertFalse(router.press(TALK))
        clock.advance(0.2)
        self.assertTrue(router.press(TALK))

        self.assertEqual(len(wake.events), 2)
        self.assertEqual(router.counters()["bounces"], 1)
        self.assertEqual(router.counters()["talk_presses"], 2)

    def test_the_two_buttons_debounce_independently(self) -> None:
        router, wake, conversation, _audit, clock = build()

        router.press(TALK)
        clock.advance(0.01)

        self.assertTrue(router.press(STOP))
        self.assertEqual(len(wake.events), 1)
        self.assertEqual(len(conversation.interrupts), 1)

    def test_a_held_talk_button_reports_its_duration_to_a_hold_hook(self) -> None:
        holds: list[float] = []
        releases: list[float] = []
        router, _wake, _conversation, audit, clock = build(
            on_talk_hold=holds.append, on_talk_release=releases.append
        )

        router.press(TALK)
        clock.advance(1.2)
        router.hold(TALK)
        clock.advance(0.3)

        self.assertAlmostEqual(router.release(TALK) or 0.0, 1.5, places=3)
        self.assertAlmostEqual(holds[0], 1.2, places=3)
        self.assertAlmostEqual(releases[0], 1.5, places=3)
        hold_events = [
            item for item in audit.events() if item["event"] == "button_hold"
        ]
        self.assertEqual(hold_events[0]["held_seconds"], 1.2)

    def test_hold_and_release_without_a_press_are_ignored(self) -> None:
        router, _wake, _conversation, audit, _clock = build()

        router.hold(TALK)

        self.assertIsNone(router.release(TALK))
        self.assertEqual(audit.events(), ())

    def test_an_unknown_button_is_rejected(self) -> None:
        router, _wake, _conversation, _audit, _clock = build()

        with self.assertRaisesRegex(ValueError, "unknown button"):
            router.press("power")

    def test_a_failing_audit_sink_never_swallows_the_press(self) -> None:
        class BrokenAudit:
            def record(self, event: object) -> None:
                raise RuntimeError("audit disk is full")

        wake = FakeWake()
        router = ButtonRouter(
            wake=wake,
            conversation=FakeConversation(),
            audit_sink=BrokenAudit(),
            wake_phrase="Miso",
            debounce_seconds=0.05,
            hold_seconds=1.0,
        )

        with self.assertLogs("miso.buttons", level="ERROR"):
            self.assertTrue(router.press(TALK))

        self.assertEqual(len(wake.events), 1)

    def test_invalid_router_settings_are_rejected(self) -> None:
        for override in ({"debounce_seconds": -1}, {"hold_seconds": 0}):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    build(**override)


class ButtonManagerTests(unittest.TestCase):
    def manager(self, **overrides: object) -> ButtonManager:
        router, _wake, _conversation, _audit, _clock = build()
        settings: dict[str, object] = {
            "enabled": True,
            "router": router,
            "talk_pin": 23,
            "stop_pin": 24,
            "pull_up": True,
            "bounce_seconds": 0.05,
            "hold_seconds": 1.0,
        }
        settings.update(overrides)
        return ButtonManager(**settings)

    def test_identical_pins_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pins must differ"):
            self.manager(stop_pin=23)

    def test_a_disabled_feature_starts_and_stops_without_hardware(self) -> None:
        manager = self.manager(enabled=False)

        manager.start()
        manager.stop()

        status = manager.status()
        self.assertFalse(status["enabled"])
        self.assertEqual(status["state"], "disabled")
        self.assertIsNone(status["last_error"])

    def test_missing_gpio_support_disables_the_feature_instead_of_raising(self) -> None:
        import builtins

        real_import = builtins.__import__

        def deny_gpiozero(name: str, *arguments: object) -> object:
            if name == "gpiozero":
                raise ImportError("No module named 'gpiozero'")
            return real_import(name, *arguments)

        manager = self.manager()
        builtins.__import__ = deny_gpiozero
        try:
            with self.assertLogs("miso.buttons", level="WARNING"):
                manager.start()
        finally:
            builtins.__import__ = real_import

        status = manager.status()
        self.assertEqual(status["state"], "unavailable")
        self.assertIn("gpiozero is not installed", str(status["last_error"]))

    def test_status_reports_the_configured_pins_and_counters(self) -> None:
        manager = self.manager(enabled=False)
        manager.router.press(TALK)

        status = manager.status()

        self.assertEqual(status["talk_pin"], 23)
        self.assertEqual(status["stop_pin"], 24)
        self.assertEqual(status["talk_presses"], 1)
        self.assertEqual(status["stop_presses"], 0)


def gpiozero_mock_factory() -> object | None:
    try:
        from gpiozero.pins.mock import MockFactory
    except ImportError:
        return None
    return MockFactory()


class MockPinFactoryTests(unittest.TestCase):
    """Drive the real gpiozero device stack over simulated pins."""

    def setUp(self) -> None:
        self.factory = gpiozero_mock_factory()
        if self.factory is None:
            self.skipTest("gpiozero is not installed")

    def tearDown(self) -> None:
        if self.factory is not None:
            self.factory.close()

    def test_simulated_pin_edges_publish_wake_and_interrupt(self) -> None:
        router, wake, conversation, _audit, _clock = build()
        manager = ButtonManager(
            enabled=True,
            router=router,
            talk_pin=23,
            stop_pin=24,
            pull_up=True,
            bounce_seconds=0.0,
            hold_seconds=1.0,
            pin_factory=self.factory,
        )
        manager.start()
        self.addCleanup(manager.stop)
        self.assertEqual(manager.status()["state"], "listening")

        talk = self.factory.pin(23)
        stop = self.factory.pin(24)
        talk.drive_low()
        talk.drive_high()
        stop.drive_low()
        stop.drive_high()

        self.assertEqual(len(wake.events), 1)
        self.assertEqual(wake.events[0].source, WAKE_SOURCE_BUTTON)
        self.assertEqual(conversation.interrupts, ["button stop"])


if __name__ == "__main__":
    unittest.main()
