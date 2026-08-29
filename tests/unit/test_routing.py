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


class SlowProvider:
    """Streams text with a gap between chunks, then completes."""

    def __init__(self, name, gap_seconds, chunk_count):
        self._name = name
        self.gap_seconds = gap_seconds
        self.chunk_count = chunk_count

    @property
    def name(self):
        return self._name

    def health(self):
        return ProviderHealth(True, "ready", "test")

    def stream(self, _request, cancel):
        for index in range(self.chunk_count):
            deadline = time.monotonic() + self.gap_seconds
            while time.monotonic() < deadline:
                if cancel.is_set():
                    raise ProviderCancelled("cancelled")
                time.sleep(0.002)
            yield ChatChunk(text=f"token{index} ")
        yield ChatChunk(done=True)


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
        reminder_tool = (
            {
                "name": "reminder_create",
                "description": "reminder_create",
                "input_schema": {"type": "object", "additionalProperties": False},
            },
        )
        routine = router.plan(
            self.request("Recuérdame comprar café", reminder_tool)
        )
        self.assertEqual(routine.classification, RouteClass.STANDARD)
        self.assertEqual(
            routine.candidates,
            ("hosted-gpt", "lan-ollama", "pi-ollama"),
        )
        complex_route = router.plan(self.request("Analyze and compare these designs"))
        self.assertEqual(complex_route.classification, RouteClass.COMPLEX)
        self.assertEqual(
            complex_route.candidates,
            ("hosted-gpt", "lan-ollama", "pi-ollama"),
        )
        self.assertIn("complexity marker", complex_route.reason)

    def test_all_request_tools_stay_available_to_the_model(self) -> None:
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
                "weather_get",
            )
        )
        router = self.router()
        for content in (
            "Set a timer",
            "Recuérdame comprar café",
            "Hello, how are you?",
            "Analyze my shopping list",
        ):
            decision = router.plan(self.request(content, tools))
            self.assertEqual(
                decision.selected_tools,
                tuple(tool["name"] for tool in tools),
            )

    def test_acknowledgement_precedes_health_and_hosted_is_preferred(self) -> None:
        router = self.router(lan=successful("lan-ollama"))
        timer_tool = (
            {
                "name": "timer_create",
                "description": "timer_create",
                "input_schema": {"type": "object", "additionalProperties": False},
            },
        )
        stream = router.stream(
            self.request("Set a timer for five minutes", timer_tool), Event()
        )
        started = time.monotonic()
        acknowledgement = next(stream)
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertIn("standard", acknowledgement.progress)
        chunks = list(stream)
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        self.assertEqual(text.route_id, acknowledgement.route_id)

    def test_request_without_matching_tool_prefers_hosted(self) -> None:
        lan = successful("lan-ollama")
        hosted = successful("hosted-gpt")
        router = self.router(lan=lan, hosted=hosted)
        chunks = list(router.stream(self.request("Compare these options"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        self.assertEqual(lan.stream_calls, 0)

    def test_tool_requests_also_prefer_hosted(self) -> None:
        tools = (
            {
                "name": "shopping_list",
                "description": "shopping_list",
                "input_schema": {"type": "object", "additionalProperties": False},
            },
        )
        pi = successful("pi-ollama")
        hosted = successful("hosted-gpt")
        router = self.router(
            pi=pi,
            lan=successful("lan-ollama"),
            hosted=hosted,
        )
        chunks = list(
            router.stream(
                self.request("Analyze my shopping list", tools),
                Event(),
            )
        )
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        self.assertEqual(pi.stream_calls, 0)

    def test_complex_request_skips_unconfigured_hosted_and_uses_pi(self) -> None:
        hosted = successful("hosted-gpt")
        hosted.available = False
        router = self.router(hosted=hosted)
        chunks = list(router.stream(self.request("Analyze this design"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "pi-ollama")

    def test_unavailable_and_failed_providers_fall_back(self) -> None:
        hosted = successful("hosted-gpt")
        hosted.available = False
        lan = FakeProvider(
            "lan-ollama",
            stream_error=ProviderError("bounded LAN failure"),
        )
        pi = successful("pi-ollama")
        router = self.router(pi=pi, lan=lan, hosted=hosted)
        chunks = list(router.stream(self.request("Set a timer"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "pi-ollama")
        finished = [
            event
            for event in self.audit.events()
            if event["event"] == "routing_finished"
        ][0]
        self.assertEqual(finished["selected_provider"], "pi-ollama")
        self.assertEqual(len(finished["failures"]), 2)

    def test_attempt_timeout_falls_back_within_bound(self) -> None:
        hosted = FakeProvider("hosted-gpt", block=True)
        router = self.router(
            pi=successful("pi-ollama"),
            hosted=hosted,
            attempt_timeout_seconds=0.03,
        )
        started = time.monotonic()
        chunks = list(router.stream(self.request("Set a timer"), Event()))
        self.assertLess(time.monotonic() - started, 0.2)
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "pi-ollama")

    def test_failure_after_visible_output_does_not_mix_providers(self) -> None:
        hosted = FakeProvider(
            "hosted-gpt",
            chunks=(ChatChunk(text="partial"),),
            stream_error=ProviderError("failed late"),
        )
        pi = successful("pi-ollama")
        router = self.router(pi=pi, hosted=hosted)
        with self.assertRaisesRegex(RoutingError, "after streaming began"):
            list(router.stream(self.request("Set a timer"), Event()))
        self.assertEqual(pi.stream_calls, 0)

    def test_fresh_health_verdict_is_reused_across_turns(self) -> None:
        hosted = successful("hosted-gpt")
        hosted.health_calls = 0
        original_health = hosted.health

        def counting_health():
            hosted.health_calls += 1
            return original_health()

        hosted.health = counting_health
        router = self.router(hosted=hosted, health_cache_seconds=60.0)
        for _ in range(3):
            list(router.stream(self.request("hello"), Event()))
        self.assertEqual(hosted.health_calls, 1)

    def test_stream_failure_evicts_the_cached_health_verdict(self) -> None:
        hosted = FakeProvider(
            "hosted-gpt", stream_error=ProviderError("boom")
        )
        hosted.health_calls = 0
        original_health = hosted.health

        def counting_health():
            hosted.health_calls += 1
            return original_health()

        hosted.health = counting_health
        router = self.router(hosted=hosted, health_cache_seconds=60.0)
        list(router.stream(self.request("hello"), Event()))
        list(router.stream(self.request("hello"), Event()))
        self.assertEqual(hosted.health_calls, 2)

    def test_unavailable_health_is_never_cached(self) -> None:
        hosted = successful("hosted-gpt")
        hosted.available = False
        hosted.health_calls = 0
        original_health = hosted.health

        def counting_health():
            hosted.health_calls += 1
            return original_health()

        hosted.health = counting_health
        router = self.router(hosted=hosted, health_cache_seconds=60.0)
        list(router.stream(self.request("hello"), Event()))
        hosted.available = True
        chunks = list(router.stream(self.request("hello"), Event()))
        text = next(chunk for chunk in chunks if chunk.text)
        self.assertEqual(text.provider, "hosted-gpt")
        self.assertEqual(hosted.health_calls, 2)

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


    def test_slow_stream_survives_when_each_gap_is_within_the_attempt_timeout(
        self,
    ) -> None:
        # Four 40ms gaps total 160ms, far past a 100ms attempt timeout. The
        # timeout bounds silence between chunks, so the answer must survive.
        router = self.router(
            pi=SlowProvider("pi-ollama", 0.04, 4),
            attempt_timeout_seconds=0.1,
            stream_timeout_seconds=5.0,
        )
        chunks = list(
            router.stream(self.request("hola"), Event(), manual_override="pi-ollama")
        )
        text = "".join(chunk.text for chunk in chunks if chunk.text)
        self.assertEqual(text, "token0 token1 token2 token3 ")

    def test_stream_that_stalls_past_the_attempt_timeout_still_fails(self) -> None:
        router = self.router(
            pi=SlowProvider("pi-ollama", 0.3, 2),
            attempt_timeout_seconds=0.05,
            stream_timeout_seconds=5.0,
        )
        with self.assertRaises(RoutingError):
            list(
                router.stream(
                    self.request("hola"), Event(), manual_override="pi-ollama"
                )
            )

    def test_total_stream_budget_bounds_an_endlessly_dribbling_provider(self) -> None:
        router = self.router(
            pi=SlowProvider("pi-ollama", 0.01, 10_000),
            attempt_timeout_seconds=0.05,
            stream_timeout_seconds=0.12,
        )
        with self.assertRaises(RoutingError) as caught:
            list(
                router.stream(
                    self.request("hola"), Event(), manual_override="pi-ollama"
                )
            )
        self.assertIn("total budget", str(caught.exception))

    def test_stream_timeout_below_attempt_timeout_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.router(attempt_timeout_seconds=10.0, stream_timeout_seconds=1.0)


if __name__ == "__main__":
    unittest.main()
