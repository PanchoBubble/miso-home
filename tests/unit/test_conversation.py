from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
import uuid

from miso.conversation import (
    ConversationError,
    ConversationManager,
    ConversationState,
)
from miso.memory import MemoryStore
from miso.providers import ChatChunk, ProviderHealth, ProviderSet
from miso.routing import ProviderRouter
from miso.speech import SpeechResult
from miso.tools import InMemoryAuditLog, ToolDefinition, ToolRegistry
from miso.transcription import SpeechActivity, TranscriptionResult
from miso.wake import WakeEvent


class EventSource:
    enabled = True

    def __init__(self) -> None:
        self.events = deque()
        self.results = deque()
        self.activity = deque()
        self.condition = threading.Condition()

    def put_wake(self) -> None:
        with self.condition:
            self.events.append(WakeEvent("Miso", 1.0, time.time()))
            self.condition.notify_all()

    def put_result(self, text: str, language: str = "en") -> None:
        with self.condition:
            self.results.append(transcript(text, language))
            self.condition.notify_all()

    def put_activity(self, kind: str = "started") -> None:
        with self.condition:
            self.activity.append(SpeechActivity(kind, time.time()))
            self.condition.notify_all()

    def get_event(self, timeout=None):
        return self._get(self.events, timeout)

    def get_result(self, timeout=None):
        return self._get(self.results, timeout)

    def get_activity(self, timeout=None):
        return self._get(self.activity, timeout)

    def _get(self, values, timeout):
        with self.condition:
            if not values and timeout:
                self.condition.wait(timeout)
            return values.popleft() if values else None


class FakeSpeech:
    enabled = True

    def __init__(self, blocked_texts=()) -> None:
        self.blocked_texts = set(blocked_texts)
        self.calls = []
        self.cancelled = []
        self.results = {}
        self.condition = threading.Condition()

    def speak(self, text, language, *, volume=None):
        request_id = str(uuid.uuid4())
        with self.condition:
            self.calls.append((request_id, text, language))
            if text not in self.blocked_texts:
                self.results[request_id] = speech_result(request_id, language)
            self.condition.notify_all()
        return request_id

    def wait(self, request_id, timeout=None):
        with self.condition:
            if request_id not in self.results:
                self.condition.wait(timeout)
            return self.results.get(request_id)

    def cancel(self, request_id=None):
        with self.condition:
            candidates = [
                item[0]
                for item in self.calls
                if item[0] not in self.results
                and (request_id is None or item[0] == request_id)
            ]
            for candidate in candidates:
                language = next(item[2] for item in self.calls if item[0] == candidate)
                self.cancelled.append(candidate)
                self.results[candidate] = speech_result(
                    candidate, language, status="cancelled"
                )
            self.condition.notify_all()
            return bool(candidates)


class FakeProvider:
    name = "pi-ollama"

    def __init__(self) -> None:
        self.fail = False

    def health(self):
        return ProviderHealth(True, "ready", "fake")

    def stream(self, request, cancel):
        if self.fail:
            raise RuntimeError("provider exploded")
        latest = request.messages[-1]["content"]
        if "tool" in latest:
            yield ChatChunk(
                tool_call={"name": "test_action", "arguments": {"value": 2}}
            )
            yield ChatChunk(done=True)
            return
        yield ChatChunk(text="Second response" if "second" in latest else "First response")
        yield ChatChunk(done=True)


def transcript(text: str, language: str = "en") -> TranscriptionResult:
    return TranscriptionResult(
        text=text,
        language=language,
        model_language=language,
        language_confidence=0.9,
        confidence=0.8,
        segments=(),
        audio_milliseconds=500,
        inference_milliseconds=100,
        real_time_factor=0.2,
        model="fake.bin",
    )


def speech_result(request_id: str, language: str, status="completed") -> SpeechResult:
    return SpeechResult(
        request_id=request_id,
        status=status,
        language=language,
        voice=f"fake-{language}",
        first_audio_milliseconds=1,
        synthesis_milliseconds=1,
        total_milliseconds=1,
        audio_milliseconds=10,
        chunks=1,
    )


def wait_for(manager: ConversationManager, state: str, timeout=1.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status()["state"] != state and time.monotonic() < deadline:
        time.sleep(0.005)
    if manager.status()["state"] != state:
        raise AssertionError(f"state did not become {state}: {manager.status()}")


class ConversationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.store = MemoryStore(Path(self.temporary.name) / "miso.sqlite3")
        self.store.migrate()
        self.source = EventSource()
        self.provider = FakeProvider()
        self.audit = InMemoryAuditLog()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manager(self, speech=None, listen=1, checkback=1, tools=None):
        router = ProviderRouter(
            ProviderSet(pi=self.provider, lan=None, hosted=None), self.audit
        )
        return ConversationManager(
            enabled=True,
            wake=self.source,
            transcription=self.source,
            router=router,
            tools=tools or ToolRegistry(self.audit),
            speech=speech or FakeSpeech(),
            memory=self.store,
            audit_sink=self.audit,
            system_prompt="You are Miso.",
            wake_phrase="Miso",
            listen_timeout_seconds=listen,
            checkback_timeout_seconds=checkback,
        )

    def test_rejects_invalid_transition(self) -> None:
        manager = self.manager()
        with self.assertRaisesRegex(ConversationError, "idle -> routing"):
            manager.transition(ConversationState.ROUTING, "invalid test")

    def test_wake_routes_response_and_opens_follow_up(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")

            self.assertEqual([item[1] for item in speech.calls], ["Yes?", "First response"])
            conversation_id = manager.status()["conversation_id"]
            events = self.store.events(str(conversation_id))
            self.assertEqual([event.role for event in events], ["user", "assistant"])
            self.assertEqual(events[0].content, "tell me hello")
            self.assertTrue(
                any(event["event"] == "conversation_transition" for event in self.audit.events())
            )
        finally:
            manager.stop()

    def test_speech_onset_cancels_output_and_routes_barge_in(self) -> None:
        speech = FakeSpeech(blocked_texts={"First response"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("first request")
            wait_for(manager, "speaking")
            self.source.put_activity()
            wait_for(manager, "transcribing")
            self.source.put_result("second request")
            wait_for(manager, "follow_up")

            self.assertTrue(speech.cancelled)
            self.assertEqual(speech.calls[-1][1], "Second response")
            self.assertEqual(manager.status()["interruptions"], 1)
        finally:
            manager.stop()

    def test_tool_turn_is_audited_stored_and_confirmed(self) -> None:
        tools = ToolRegistry(self.audit)
        tools.register(
            ToolDefinition(
                name="test_action",
                description="Test a voice action.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=lambda arguments, _context: {"doubled": arguments["value"] * 2},
            )
        )
        speech = FakeSpeech()
        manager = self.manager(speech, tools=tools)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("run tool")
            wait_for(manager, "follow_up")

            conversation_id = str(manager.status()["conversation_id"])
            events = self.store.events(conversation_id)
            tool_event = next(event for event in events if event.kind == "tool")
            self.assertEqual(tool_event.content, "test_action")
            self.assertTrue(tool_event.payload["ok"])
            self.assertEqual(speech.calls[-1][1], "Done.")
            transitions = [
                event["current"]
                for event in self.audit.events()
                if event["event"] == "conversation_transition"
            ]
            self.assertIn("using_tool", transitions)
        finally:
            manager.stop()

    def test_explicit_spanish_goodbye_closes_follow_up(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("adiós", "es")
            wait_for(manager, "idle")

            self.assertEqual(speech.calls[-1][1:], ("Hasta luego.", "es"))
            self.assertIsNone(manager.status()["conversation_id"])
        finally:
            manager.stop()

    def test_timeout_checks_back_then_says_goodbye_and_returns_idle(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, listen=0.05, checkback=0.05)
        manager.start()
        try:
            self.source.put_wake()
            deadline = time.monotonic() + 0.5
            while not speech.calls and time.monotonic() < deadline:
                time.sleep(0.005)
            wait_for(manager, "idle", timeout=1)
            self.assertEqual(
                [item[1] for item in speech.calls],
                ["Yes?", "Anything else?", "Goodbye."],
            )
            self.assertEqual(manager.status()["timeouts"], 2)
            self.assertIsNone(manager.status()["conversation_id"])
        finally:
            manager.stop()

    def test_provider_error_recovers_safely_to_idle(self) -> None:
        self.provider.fail = True
        manager = self.manager()
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("fail please")
            wait_for(manager, "idle")
            self.assertEqual(manager.status()["errors"], 1)
            self.assertIn("provider", str(manager.status()["last_error"]))
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
