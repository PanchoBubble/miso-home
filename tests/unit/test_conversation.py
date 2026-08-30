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

    def activate(self, event: WakeEvent) -> None:
        with self.condition:
            self.events.append(event)
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
        self.block = False
        self.partial = False
        self.entered = threading.Event()

    def health(self):
        return ProviderHealth(True, "ready", "fake")

    def stream(self, request, cancel):
        if self.fail:
            raise RuntimeError("provider exploded")
        if self.partial:
            yield ChatChunk(text="Half an answer")
            raise RuntimeError("provider died mid-answer")
        if self.block:
            self.entered.set()
            cancel.wait(1)
            if cancel.is_set():
                return
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

    def manager(
        self,
        speech=None,
        listen=1,
        checkback=1,
        tools=None,
        fast_lane=None,
        tool_picker=None,
    ):
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
            fast_lane=fast_lane,
            tool_picker=tool_picker,
            audit_sink=self.audit,
            system_prompt="You are Miso.",
            wake_phrase="Miso",
            listen_timeout_seconds=listen,
            checkback_timeout_seconds=checkback,
        )

    def test_tool_picker_answers_when_the_fast_lane_misses(self) -> None:
        from miso.intake import FastLane
        from miso.toolpick import ToolPicker
        from miso.tools import register_household_tools

        class StubCompletion:
            def complete_json(self, system, user, **_options) -> str:
                return '{"tool": "timer_create", "arguments": {"duration_seconds": 300}}'

        registry = ToolRegistry(self.audit)
        register_household_tools(
            registry, Path(self.temporary.name) / "picker.sqlite3"
        )
        self.provider.fail = True
        speech = FakeSpeech()
        manager = self.manager(
            speech,
            tools=registry,
            fast_lane=FastLane(registry, self.audit),
            tool_picker=ToolPicker(registry, StubCompletion(), self.audit),
        )
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, get a countdown going for five minutes")
            wait_for(manager, "follow_up")

            self.assertEqual(
                [item[1] for item in speech.calls],
                ["Yes?", "Timer set for 5 minutes."],
            )
            picks = [
                event for event in self.audit.events() if event["event"] == "tool_pick"
            ]
            self.assertEqual([event["status"] for event in picks], ["picked"])
        finally:
            manager.stop()

    def test_fast_lane_answers_without_consulting_a_provider(self) -> None:
        from miso.intake import FastLane
        from miso.tools import register_household_tools

        registry = ToolRegistry(self.audit)
        register_household_tools(
            registry, Path(self.temporary.name) / "fastlane.sqlite3"
        )
        self.provider.fail = True
        speech = FakeSpeech()
        manager = self.manager(
            speech,
            tools=registry,
            fast_lane=FastLane(registry, self.audit),
        )
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, set a timer for 5 minutes")
            wait_for(manager, "follow_up")

            self.assertEqual(
                [item[1] for item in speech.calls],
                ["Yes?", "Timer set for 5 minutes."],
            )
            conversation_id = manager.status()["conversation_id"]
            events = self.store.events(str(conversation_id))
            self.assertEqual(
                [(event.kind, event.role) for event in events],
                [("message", "user"), ("tool", "assistant"), ("message", "assistant")],
            )
            self.assertEqual(events[1].content, "timer_create")
        finally:
            manager.stop()

    def test_streamed_sentences_are_spoken_before_the_answer_completes(self) -> None:
        class SentenceProvider:
            name = "pi-ollama"

            def health(self):
                return ProviderHealth(True, "ready", "fake")

            def stream(self, request, cancel):
                for piece in ("First sentence. Sec", "ond sentence. And", " a third."):
                    yield ChatChunk(text=piece)
                yield ChatChunk(done=True)

        self.provider = SentenceProvider()
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me a story")
            wait_for(manager, "follow_up")

            self.assertEqual(
                [item[1] for item in speech.calls],
                ["Yes?", "First sentence.", "Second sentence.", "And a third."],
            )
            conversation_id = manager.status()["conversation_id"]
            events = self.store.events(str(conversation_id))
            self.assertEqual(
                events[-1].content,
                "First sentence. Second sentence. And a third.",
            )
        finally:
            manager.stop()

    def test_transcript_listener_receives_what_miso_heard(self) -> None:
        manager = self.manager()
        heard = []
        manager.add_transcript_listener(lambda text, language: heard.append((text, language)))
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")
            self.assertEqual(heard, [("tell me hello", "en")])
        finally:
            manager.stop()

    def test_error_listener_receives_failure_reason(self) -> None:
        self.provider.fail = True
        manager = self.manager()
        errors = []
        manager.add_error_listener(errors.append)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")
            self.assertEqual(len(errors), 1)
            self.assertIn("failed", errors[0])
        finally:
            manager.stop()

    def test_rejects_invalid_transition(self) -> None:
        manager = self.manager()
        with self.assertRaisesRegex(ConversationError, "idle -> routing"):
            manager.transition(ConversationState.ROUTING, "invalid test")

    def test_transition_listener_receives_committed_state(self) -> None:
        manager = self.manager()
        transitions = []
        manager.add_transition_listener(transitions.append)

        manager.transition(ConversationState.ACKNOWLEDGING, "display test")

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].previous, ConversationState.IDLE)
        self.assertEqual(transitions[0].current, ConversationState.ACKNOWLEDGING)

    def test_wake_routes_response_and_opens_follow_up(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        responses = []
        drafts = []
        manager.add_response_listener(
            lambda text, language, final: (
                responses if final else drafts
            ).append((text, language))
        )
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")

            self.assertEqual([item[1] for item in speech.calls], ["Yes?", "First response"])
            self.assertEqual(responses, [("Yes?", "en"), ("First response", "en")])
            self.assertEqual(drafts, [("First response", "en")])
            conversation_id = manager.status()["conversation_id"]
            events = self.store.events(str(conversation_id))
            self.assertEqual([event.role for event in events], ["user", "assistant"])
            self.assertEqual(events[0].content, "tell me hello")
            self.assertTrue(
                any(event["event"] == "conversation_transition" for event in self.audit.events())
            )
        finally:
            manager.stop()

    def test_idle_transcription_of_miso_uses_wake_fallback(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_result("Miso.")
            wait_for(manager, "listening")

            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
        finally:
            manager.stop()

    def test_idle_transcription_without_leading_miso_stays_idle(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_result("The miso soup is ready")
            time.sleep(0.1)

            self.assertEqual(manager.status()["state"], "idle")
            self.assertEqual(speech.calls, [])
        finally:
            manager.stop()

    def test_wake_phrase_interrupts_a_spoken_answer(self) -> None:
        # Wake-word barge-in survives the echo guard: openWakeWord is far more
        # selective than the VAD, so it does not fire on Miso's own voice.
        speech = FakeSpeech(blocked_texts={"First response"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("first request")
            wait_for(manager, "speaking")

            self.source.put_wake()
            # The fake speech backend completes instantly, so the acknowledging
            # state is transient; assert the outcome rather than racing it.
            wait_for(manager, "listening")

            self.assertTrue(speech.cancelled)
            self.assertGreaterEqual(manager.status()["interruptions"], 1)
            self.assertIn("Yes?", [item[1] for item in speech.calls])
            self.assertEqual(manager.status()["interruptions"], 1)
        finally:
            manager.stop()

    def test_noise_while_thinking_does_not_cancel_the_turn(self) -> None:
        # The regression this guards: a mic onset during ROUTING cancelled the
        # in-flight answer. On a Pi that thinks for seconds, an impatient repeat
        # or a room noise then left the request permanently unanswered.
        self.provider.block = True
        speech = FakeSpeech()
        manager = self.manager(speech, listen=1)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("first request")
            self.assertTrue(self.provider.entered.wait(1))
            wait_for(manager, "routing")

            self.source.put_activity("started")
            self.source.put_activity("discarded")
            time.sleep(0.2)
            self.assertEqual(manager.status()["state"], "routing")
            self.assertEqual(manager.status()["interruptions"], 0)

            wait_for(manager, "follow_up", timeout=4)
            self.assertEqual(manager.status()["errors"], 0)
            self.assertIn("First response", [item[1] for item in speech.calls])
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

    def test_tool_summary_is_spoken_instead_of_generic_confirmation(self) -> None:
        tools = ToolRegistry(self.audit)
        tools.register(
            ToolDefinition(
                name="test_action",
                description="Return a speakable result.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=lambda _arguments, _context: {
                    "summary": "It is cloudy and 18 degrees."
                },
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

            self.assertEqual(speech.calls[-1][1], "It is cloudy and 18 degrees.")
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
    def wait_for_cue(self, speech, text, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(item[1] == text for item in speech.calls):
                return
            time.sleep(0.005)
        raise AssertionError(f"cue {text!r} was never spoken: {speech.calls}")

    def test_cue_heard_by_the_microphone_does_not_become_a_request(self) -> None:
        # The Pi speaker shares a room with the mic and there is no echo
        # cancellation, so the VAD hears Miso say "Yes?" and used to route it
        # as the user's request, swallowing the real one.
        speech = FakeSpeech(blocked_texts={"Yes?"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            self.wait_for_cue(speech, "Yes?")

            self.source.put_activity()
            self.source.put_result("Yes")
            time.sleep(0.2)

            status = manager.status()
            self.assertEqual(status["state"], "acknowledging")
            self.assertEqual(status["interruptions"], 0)
            self.assertEqual(status["turns"], 0)
            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
        finally:
            manager.stop()

    def test_real_request_after_the_cue_is_still_heard(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            time.sleep(0.7)  # let the post-cue echo guard lapse

            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")

            self.assertEqual(
                [item[1] for item in speech.calls], ["Yes?", "First response"]
            )
            self.assertEqual(manager.status()["turns"], 1)
        finally:
            manager.stop()

    def test_provider_failing_mid_answer_still_speaks_what_it_produced(self) -> None:
        self.provider.partial = True
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            time.sleep(0.7)
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")

            self.assertEqual(
                [item[1] for item in speech.calls], ["Yes?", "Half an answer"]
            )
            self.assertEqual(manager.status()["errors"], 0)
        finally:
            manager.stop()

    def test_provider_failing_before_any_text_still_recovers_to_idle(self) -> None:
        self.provider.fail = True
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            time.sleep(0.7)
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")

            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
            self.assertGreaterEqual(manager.status()["errors"], 1)
        finally:
            manager.stop()

    def test_mic_hearing_the_answer_does_not_truncate_it(self) -> None:
        # The regression this guards: the VAD hears Miso's own answer through
        # the speaker, cancels playback, clears the buffer, and only the tail of
        # the phrase reaches the listener.
        speech = FakeSpeech(blocked_texts={"First response"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            time.sleep(0.7)
            self.source.put_result("first request")
            wait_for(manager, "speaking")

            self.source.put_activity()
            self.source.put_result("First response")
            time.sleep(0.25)

            self.assertEqual(manager.status()["state"], "speaking")
            self.assertEqual(speech.cancelled, [])
            self.assertEqual(manager.status()["interruptions"], 0)
        finally:
            manager.stop()

    def test_transcript_arriving_mid_answer_never_cancels_playback(self) -> None:
        # The regression this guards: a transcript delivered while Miso was
        # speaking called _cancel_active(), which cleared the playback buffer,
        # so only the tail of the phrase reached the speaker.
        speech = FakeSpeech(blocked_texts={"First response"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("first request")
            wait_for(manager, "speaking")

            self.source.put_result("some noise the mic picked up")
            time.sleep(0.25)

            self.assertEqual(manager.status()["state"], "speaking")
            self.assertEqual(speech.cancelled, [])
        finally:
            manager.stop()

    def test_miso_hearing_its_own_words_is_not_treated_as_a_request(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")

            # Whisper returns Miso's own acknowledgement, heard via the speaker.
            self.source.put_result("Yes")
            time.sleep(0.25)

            self.assertEqual(manager.status()["turns"], 0)
            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
