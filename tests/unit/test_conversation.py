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
    _SegmentSpeaker,
    _split_long_clause,
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
        self.gate_open = False
        self.gate_history = []
        self.warmed = 0

    def open_gate(self) -> None:
        self.gate_open = True
        self.gate_history.append(True)

    def close_gate(self) -> None:
        self.gate_open = False
        self.gate_history.append(False)

    def warm_up(self) -> None:
        self.warmed += 1

    def put_wake(self, source: str = "model") -> None:
        with self.condition:
            self.events.append(WakeEvent("Miso", 1.0, time.time(), source=source))
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
        # Only a reply that asks something keeps the microphone open, so a
        # test that needs a second turn has to make Miso ask for one.
        self.question = False
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
        answer = "Second response" if "second" in latest else "First response"
        if self.question:
            answer = f"{answer}. Anything else?"
        yield ChatChunk(text=answer)
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


def wait_until(predicate, description: str, timeout=1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {description}")


def wait_for(manager: ConversationManager, state: str, timeout=1.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.status()["state"] != state and time.monotonic() < deadline:
        time.sleep(0.005)
    if manager.status()["state"] != state:
        raise AssertionError(f"state did not become {state}: {manager.status()}")


class SegmentSplitTests(unittest.TestCase):
    def test_a_long_sentence_is_broken_at_its_clause(self) -> None:
        head, rest = _split_long_clause(
            "The sky looks blue because air molecules scatter blue sunlight more "
            "strongly than other colours, and that scattered light reaches your eyes"
        )
        self.assertTrue(head.endswith("colours,"))
        self.assertTrue(rest.startswith("and that scattered"))

    def test_a_short_sentence_is_left_whole(self) -> None:
        self.assertEqual(
            _split_long_clause("Timer set for five minutes."),
            ("", "Timer set for five minutes."),
        )

    def test_a_long_sentence_without_punctuation_is_left_whole(self) -> None:
        text = " ".join(["word"] * 30)
        self.assertEqual(_split_long_clause(text), ("", text))

    def test_a_tiny_leading_clause_is_kept_attached(self) -> None:
        # "Sure," alone would be a stutter, so it stays with what follows.
        text = (
            "Sure, the timer is running and it has about four minutes left "
            "before it goes off in the kitchen"
        )
        head, rest = _split_long_clause(text)
        self.assertEqual(head, "")
        self.assertEqual(rest, text)


class SegmentSpeakerTests(unittest.TestCase):
    class _Recorder:
        def __init__(self) -> None:
            self.spoken = []

        def _speak_segment(self, speaker, segment):
            self.spoken.append(segment)

    def _speaker(self, spoken):
        speaker = _SegmentSpeaker.__new__(_SegmentSpeaker)
        speaker._buffer = ""
        speaker.received = False
        speaker.opened_gate = False
        speaker._speak = spoken.append
        return speaker

    def test_a_long_first_sentence_speaks_before_it_ends(self) -> None:
        spoken = []
        speaker = self._speaker(spoken)
        speaker.feed(
            "The sky looks blue because air molecules scatter blue sunlight more "
            "strongly than other colours, and that scattered light reaches your eyes "
        )

        # Audio starts on the first clause instead of waiting for the full stop.
        self.assertEqual(len(spoken), 1)
        self.assertTrue(spoken[0].endswith("colours,"))

    def test_sentences_still_win_over_clauses(self) -> None:
        spoken = []
        speaker = self._speaker(spoken)
        speaker.feed("Timer set for five minutes. Anything else? ")
        self.assertEqual(
            spoken, ["Timer set for five minutes.", "Anything else?"]
        )

    def test_a_short_answer_is_spoken_once_at_the_end(self) -> None:
        spoken = []
        speaker = self._speaker(spoken)
        speaker.feed("Timer set")
        speaker.feed(" for five minutes")
        self.assertEqual(spoken, [])
        speaker.finish()
        self.assertEqual(spoken, ["Timer set for five minutes"])


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
        latency=None,
        acknowledge_wake=True,
        languages=("en", "es"),
        echo_guard=0.6,
        follow_up=1,
        session=45,
        maximum_discards=3,
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
            latency_sink=latency,
            acknowledge_wake=acknowledge_wake,
            languages=languages,
            echo_guard_seconds=echo_guard,
            system_prompt="You are Miso.",
            wake_phrase="Miso",
            listen_timeout_seconds=listen,
            checkback_timeout_seconds=checkback,
            follow_up_timeout_seconds=follow_up,
            session_timeout_seconds=session,
            maximum_discards=maximum_discards,
        )

    def test_the_recogniser_only_runs_while_miso_is_expecting_speech(self) -> None:
        manager = self.manager()
        manager.start()
        try:
            self.assertFalse(self.source.gate_open)
            self.source.put_wake()
            wait_for(manager, "listening")
            self.assertTrue(self.source.gate_open)

            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")
            self.assertFalse(self.source.gate_open)
        finally:
            manager.stop()

    def test_a_wake_warms_the_transcription_lane(self) -> None:
        manager = self.manager()
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            wait_until(lambda: self.source.warmed >= 1, "the lane to be warmed")
        finally:
            manager.stop()

    def test_a_button_talk_warms_the_transcription_lane(self) -> None:
        manager = self.manager()
        manager.start()
        try:
            self.source.put_wake(source="button")
            wait_for(manager, "listening")
            wait_until(lambda: self.source.warmed >= 1, "the lane to be warmed")
        finally:
            manager.stop()

    def test_a_plain_answer_closes_the_microphone(self) -> None:
        # The regression this guards: every answer reopened an eight second
        # listening window, and any noise inside it reopened another, so the
        # microphone never closed and the room's conversation became Miso's.
        manager = self.manager()
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")

            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["reason"], "answer completed")
        finally:
            manager.stop()

    def test_an_answer_that_asks_something_keeps_listening(self) -> None:
        self.provider.question = True
        manager = self.manager(follow_up=5)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")

            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["reason"], "question awaiting an answer")
        finally:
            manager.stop()

    def test_a_tool_turn_closes_even_when_it_ends_in_a_question(self) -> None:
        tools = ToolRegistry(self.audit)
        tools.register(
            ToolDefinition(
                name="test_action",
                description="Return a result that ends in a question.",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                handler=lambda _arguments, _context: {
                    "summary": "Timer set. Anything else?"
                },
            )
        )
        manager = self.manager(tools=tools)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, run the tool")
            wait_for(manager, "idle")

            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["reason"], "tool completed")
        finally:
            manager.stop()

    def test_repeated_noise_stops_re_arming_the_listening_window(self) -> None:
        # Each discarded transcript used to buy another full listening window,
        # which is what let a conversation in the next room hold the
        # microphone open indefinitely.
        manager = self.manager(listen=30, maximum_discards=3, echo_guard=0)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            for _ in range(3):
                self.source.put_activity()
                wait_for(manager, "transcribing")
                self.source.put_result("bonjour tout le monde", language="fr")
            wait_for(manager, "idle")

            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["reason"], "unsupported language ignored")
        finally:
            manager.stop()

    def test_the_session_budget_closes_a_window_noise_keeps_re_arming(self) -> None:
        manager = self.manager(listen=30, session=5, maximum_discards=50)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            deadline = manager._deadline
            self.assertIsNotNone(deadline)
            # The listening window is thirty seconds, but the session it lives
            # inside is five, and the window may never outlast it.
            self.assertLess(deadline - time.monotonic(), 5.1)
        finally:
            manager.stop()

    def test_dismissals_end_the_session_without_matching_the_whole_transcript(
        self,
    ) -> None:
        for phrase in ("that's all", "stop listening", "ya está", "gracias"):
            with self.subTest(phrase=phrase):
                self.assertTrue(ConversationManager._is_goodbye(phrase))
        for phrase in ("ok", "vale", "stop the timer", "thanks for the timer"):
            with self.subTest(phrase=phrase):
                self.assertFalse(ConversationManager._is_goodbye(phrase))

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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")
            self.assertEqual(heard, [("tell me hello", "en")])
        finally:
            manager.stop()

    def test_capture_listener_cues_the_display_before_a_transcript_exists(
        self,
    ) -> None:
        manager = self.manager(echo_guard=0)
        cues = []
        manager.add_capture_listener(cues.append)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            wait_until(lambda: cues == ["capturing"], "capture cue")
            self.source.put_activity("ended")
            wait_until(
                lambda: cues == ["capturing", "transcribing"], "transcribing cue"
            )
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")
            self.assertEqual(cues, ["capturing", "transcribing"])
        finally:
            manager.stop()

    def test_capture_cue_is_retired_when_the_utterance_is_discarded(self) -> None:
        manager = self.manager(echo_guard=0)
        cues = []
        manager.add_capture_listener(cues.append)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            wait_for(manager, "transcribing")
            self.source.put_activity("discarded")
            wait_for(manager, "listening")
            self.assertEqual(cues, ["capturing", "discarded"])
        finally:
            manager.stop()

    def test_turn_first_audio_latency_is_recorded_separately_from_tools(self) -> None:
        latency = InMemoryAuditLog()
        manager = self.manager(latency=latency, listen=5, checkback=5, echo_guard=0)
        self.provider.question = True
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "follow_up")
            self.source.put_activity()
            self.source.put_result("second question")
            wait_until(
                lambda: sum(
                    event["event"] == "turn_first_audio"
                    for event in latency.events()
                )
                == 2,
                "both turns to report first audio",
            )
        finally:
            manager.stop()

        recorded = [
            event
            for event in latency.events()
            if event["event"] == "turn_first_audio"
        ]
        self.assertEqual([event["turn"] for event in recorded], [1, 2])
        self.assertEqual([event["origin"] for event in recorded], ["wake", "follow_up"])
        self.assertEqual([event["lane"] for event in recorded], ["model", "model"])
        self.assertEqual(recorded[0]["transcription_ms"], 100)
        self.assertEqual(recorded[0]["language"], "en")
        self.assertEqual(recorded[0]["status"], "completed")
        for event in recorded:
            self.assertGreaterEqual(event["latency_ms"], 0)
        # The tool audit trail must not grow a routing metric by accident.
        self.assertNotIn("turn_first_audio", str(self.audit.events()))

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
            wait_for(manager, "idle")

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

            wait_for(manager, "idle", timeout=4)
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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")

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
            wait_for(manager, "idle")

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

    def test_microphone_onset_while_speaking_is_ignored(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            manager._open_echo_gate()
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            time.sleep(0.15)

            # The onset never opened a capture, so the state never advanced.
            self.assertEqual(manager.status()["state"], "listening")
        finally:
            manager.stop()

    def test_microphone_reopens_once_the_echo_gate_expires(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            manager._open_echo_gate()
            manager._close_echo_gate()
            self.source.put_wake()
            wait_for(manager, "listening")
            time.sleep(manager.echo_guard_seconds + 0.1)
            self.source.put_activity()
            wait_for(manager, "transcribing")

            self.assertEqual(manager.status()["state"], "transcribing")
        finally:
            manager.stop()

    def test_garbled_echo_of_its_own_answer_is_discarded(self) -> None:
        # Both transcripts are what the Pi actually recorded of Miso's own
        # replies while the speaker was still playing them.
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            spoken = (
                "Air molecules scatter the shorter blue wavelengths of sunlight "
                "more strongly than longer wavelengths"
            )
            manager._remember_spoken(spoken)
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result(
                "We'll scatter the shorter blue wavelengths of sunlight more "
                "strongly than longer wavelengths"
            )
            wait_for(manager, "listening")

            self.assertEqual(speech.calls, [])
            conversation_id = manager.status()["conversation_id"]
            self.assertEqual(self.store.events(str(conversation_id)), [])
        finally:
            manager.stop()

    def test_a_real_follow_up_reusing_miso_words_still_routes(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            manager._remember_spoken(
                "The sky looks blue because air molecules scatter blue sunlight"
            )
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("set a timer for five minutes")
            wait_for(manager, "idle")

            events = self.store.events(str(manager.status()["conversation_id"]))
            self.assertEqual(events[0].content, "set a timer for five minutes")
        finally:
            manager.stop()

    def test_short_transcripts_are_not_treated_as_echoes(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            manager._remember_spoken("Timer set for five minutes")
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("the timer")
            wait_for(manager, "idle")

            events = self.store.events(str(manager.status()["conversation_id"]))
            self.assertEqual(events[0].content, "the timer")
        finally:
            manager.stop()

    def test_wake_without_acknowledgement_opens_the_microphone_immediately(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            started = time.monotonic()
            self.source.put_wake()
            wait_for(manager, "listening")
            elapsed = time.monotonic() - started

            # Nothing is spoken over the tail of "Miso, tell me hello".
            self.assertEqual(speech.calls, [])
            self.assertLess(elapsed, 0.3)
            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["previous"], "idle")
            self.assertEqual(latest["reason"], "wake detected")
        finally:
            manager.stop()

    def test_wake_without_acknowledgement_still_routes_the_utterance(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=False)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("Miso, tell me hello")
            wait_for(manager, "idle")

            self.assertEqual([item[1] for item in speech.calls], ["First response"])
            events = self.store.events(str(manager.status()["conversation_id"]))
            self.assertEqual(events[0].content, "tell me hello")
        finally:
            manager.stop()

    def test_wake_acknowledgement_is_spoken_when_it_is_enabled(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, acknowledge_wake=True)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["previous"], "acknowledging")
        finally:
            manager.stop()

    def test_transcript_in_an_unsupported_language_is_discarded(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("bore da sut mae", language="cy")
            wait_for(manager, "listening")

            self.assertEqual([item[1] for item in speech.calls], ["Yes?"])
            conversation_id = manager.status()["conversation_id"]
            self.assertEqual(self.store.events(str(conversation_id)), [])
        finally:
            manager.stop()

    def test_configured_language_is_kept(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech, languages=("en", "es", "cy"))
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_activity()
            self.source.put_result("bore da sut mae", language="cy")
            wait_for(manager, "idle")

            events = self.store.events(str(manager.status()["conversation_id"]))
            self.assertEqual([event.role for event in events], ["user", "assistant"])
        finally:
            manager.stop()

    def test_button_talk_opens_listening_without_an_acknowledgement(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            started = time.monotonic()
            self.source.put_wake(source="button")
            wait_for(manager, "listening")
            elapsed = time.monotonic() - started

            self.assertEqual(speech.calls, [])
            self.assertLess(elapsed, 0.3)
            latest = manager.status()["latest_transition"]
            self.assertEqual(latest["previous"], "idle")
            self.assertEqual(latest["reason"], "button talk")
        finally:
            manager.stop()

    def test_button_talk_still_routes_the_utterance_it_opened(self) -> None:
        speech = FakeSpeech()
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake(source="button")
            wait_for(manager, "listening")
            self.source.put_result("first request")
            wait_for(manager, "idle")

            self.assertEqual([item[1] for item in speech.calls], ["First response"])
        finally:
            manager.stop()

    def test_interrupt_halts_a_spoken_answer_and_returns_to_idle(self) -> None:
        speech = FakeSpeech(blocked_texts={"First response"})
        manager = self.manager(speech)
        manager.start()
        try:
            self.source.put_wake()
            wait_for(manager, "listening")
            self.source.put_result("first request")
            wait_for(manager, "speaking")

            self.assertTrue(manager.interrupt("button stop"))
            wait_for(manager, "idle")

            self.assertTrue(speech.cancelled)
            self.assertEqual(manager.status()["interruptions"], 1)
            self.assertIsNone(manager.status()["conversation_id"])
        finally:
            manager.stop()

    def test_interrupt_reports_that_an_idle_assistant_had_nothing_to_stop(self) -> None:
        manager = self.manager()
        manager.start()
        try:
            self.assertFalse(manager.interrupt("button stop"))
            self.assertEqual(manager.status()["interruptions"], 0)
        finally:
            manager.stop()


if __name__ == "__main__":
    unittest.main()
