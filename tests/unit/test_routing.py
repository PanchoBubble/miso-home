from threading import Event
import time
import unittest

from miso.providers import (
    ChatChunk,
    ChatRequest,
    ProviderError,
    ProviderHealth,
    ProviderSet,
    ProviderCancelled,
)
from miso.routing import ProviderRouter, RouteClass, RoutingError
from miso.tools import InMemoryAuditLog


class FakeProvider:
    def __init__(
        self,
        name,
        *,
        available=True,
        chunks=(),
        stream_error=None,
        block=False,
    ):
        self._name = name
        self.available = available
        self.chunks = chunks
        self.stream_error = stream_error
        self.block = block
        self.stream_calls = 0

    @property
    def name(self):
        return self._name

    def health(self):
        return ProviderHealth(
            self.available,
            "ready" if self.available else "offline",
            "test",
        )

    def stream(self, _request, cancel):
        self.stream_calls += 1
        if self.block:
            while not cancel.is_set():
                time.sleep(0.002)
            raise ProviderError("cancelled by deadline")
        for chunk in self.chunks:
            yield chunk
        if self.stream_error is not None:
            raise self.stream_error


def successful(name):
    return FakeProvider(
        name,
        chunks=(ChatChunk(text=f"from {name}"), ChatChunk(done=True)),
    )


class ProviderRouterTests(unittest.TestCase):
    def router(self, pi=None, lan=None, hosted=None, **timeouts):
        self.audit = InMemoryAuditLog()
        return ProviderRouter(
            ProviderSet(
                pi=pi or successful("pi-ollama"),
                lan=lan,
                hosted=hosted or successful("hosted-gpt"),
            ),
            self.audit,
            **timeouts,
        )

    @staticmethod
    def request(content, tools=()):
        return ChatRequest(
            messages=({"role": "user", "content": content},),
            tools=tools,
        )

    def test_classifier_and_plan_are_explainable(self) -> None:
        router = self.router(lan=successful("lan-ollama"))
        routine = router.plan(self.request("Recuérdame comprar café"))
        self.assertEqual(routine.classification, RouteClass.ROUTINE)
        self.assertEqual(
            routine.candidates,
            ("pi-ollama", "lan-ollama", "hosted-gpt"),
        )
        complex_route = router.plan(self.request("Analyze and compare these designs"))
        self.assertEqual(complex_route.classification, RouteClass.COMPLEX)
        self.assertEqual(
            complex_route.candidates,
            ("lan-ollama", "hosted-gpt", "pi-ollama"),
        )
        self.assertIn("complexity marker", complex_route.reason)

    def test_routine_request_exposes_only_relevant_tool_family(self) -> None:
        tools = tuple(
            {
                "name": name,
                "description": name,
                "input_schema": {"type": "object", "additionalProperties": False},
            }
            for name in (
                "timer_create",
                "timer_cancel",
                "reminder_create",
                "shopping_add",
                "calendar_list",
                "calendar_event_create",
            )
        )
        router = self.router()
        timer = router.plan(self.request("Set a timer", tools))
        self.assertEqual(
            timer.selected_tools,
            ("timer_create", "timer_cancel"),
        )
        reminder = router.plan(self.request("Recuérdame comprar café", tools))
        self.assertEqual(reminder.selected_tools, ("reminder_create",))
        standard = router.plan(self.request("Hello, how are you?", tools))
        self.assertEqual(standard.selected_tools, ())
        complex_shopping = router.plan(
            self.request("Analyze my shopping list", tools)
        )
        self.assertEqual(complex_shopping.selected_tools, ("shopping_add",))
        calendar = router.plan(self.request("Añade una cita al calendario", tools))
        self.assertEqual(
            calendar.selected_tools,
            ("calendar_list", "calendar_event_create"),
        )

    def test_acknowledgement_precedes_health_and_routine_prefers_pi(self) -> None:
        router = self.router(lan=successful("lan-ollama"))
        stream = router.stream(self.request("Set a timer for five minutes"), Event())
        started = time.monotonic()
        acknowledgement = next(stream)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertIn("routine", acknowledgement.progress)
        chunks = list(stream)
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "pi-ollama")
        self.assertEqual(text.route_id, acknowledgement.route_id)

    def test_complex_request_prefers_lan(self) -> None:
        lan = successful("lan-ollama")
        router = self.router(lan=lan)
        chunks = list(router.stream(self.request("Compare these options"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "lan-ollama")

    def test_complex_request_skips_unconfigured_hosted_and_uses_pi(self) -> None:
        hosted = successful("hosted-gpt")
        hosted.available = False
        router = self.router(hosted=hosted)
        chunks = list(router.stream(self.request("Analyze this design"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "pi-ollama")

    def test_unavailable_and_failed_providers_fall_back(self) -> None:
        pi = successful("pi-ollama")
        pi.available = False
        lan = FakeProvider(
            "lan-ollama",
            stream_error=ProviderError("bounded LAN failure"),
        )
        hosted = successful("hosted-gpt")
        router = self.router(pi=pi, lan=lan, hosted=hosted)
        chunks = list(router.stream(self.request("hello"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        finished = [
            event
            for event in self.audit.events()
            if event["event"] == "routing_finished"
        ][0]
        self.assertEqual(finished["selected_provider"], "hosted-gpt")
        self.assertEqual(len(finished["failures"]), 2)

    def test_attempt_timeout_falls_back_within_bound(self) -> None:
        pi = FakeProvider("pi-ollama", block=True)
        router = self.router(
            pi=pi,
            hosted=successful("hosted-gpt"),
            attempt_timeout_seconds=0.03,
        )
        started = time.monotonic()
        chunks = list(router.stream(self.request("hello"), Event()))
        self.assertLess(time.monotonic() - started, 0.2)
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")

    def test_failure_after_visible_output_does_not_mix_providers(self) -> None:
        pi = FakeProvider(
            "pi-ollama",
            chunks=(ChatChunk(text="partial"),),
            stream_error=ProviderError("failed late"),
        )
        hosted = successful("hosted-gpt")
        router = self.router(pi=pi, hosted=hosted)
        with self.assertRaisesRegex(RoutingError, "after streaming began"):
            list(router.stream(self.request("hello"), Event()))
        self.assertEqual(hosted.stream_calls, 0)

    def test_manual_override_is_strict_by_default(self) -> None:
        hosted = successful("hosted-gpt")
        router = self.router(lan=successful("lan-ollama"), hosted=hosted)
        chunks = list(
            router.stream(
                self.request("Set a timer"),
                Event(),
                manual_override="hosted-gpt",
            )
        )
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        with self.assertRaisesRegex(RoutingError, "unknown provider override"):
            router.plan(self.request("hello"), manual_override="missing")

    def test_cancellation_stops_a_routed_attempt(self) -> None:
        cancel = Event()
        router = self.router(pi=FakeProvider("pi-ollama", block=True))
        stream = router.stream(self.request("hello"), cancel)
        next(stream)
        next(stream)
        next(stream)
        cancel.set()
        with self.assertRaises(ProviderCancelled):
            next(stream)


if __name__ == "__main__":
    unittest.main()
