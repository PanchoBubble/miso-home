"""Explicit state machine for offline conversational voice turns."""

from __future__ import annotations

import logging
import re
import threading
import time
from difflib import SequenceMatcher
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from miso.intake import FastLane
from miso.memory import MemoryStore
from miso.identity import VOICE_ACTOR
from miso.providers import ChatRequest, ProviderCancelled
from miso.routing import ProviderRouter, RoutingError
from miso.speech import SpeechManager, SpeechResult
from miso.toolpick import ToolPicker
from miso.tools import ToolRegistry
from miso.tools.audit import AuditSink, audit_event
from miso.transcription import SpeechActivity, TranscriptionResult
from miso.wake import WAKE_SOURCE_BUTTON, WakeEvent


LOGGER = logging.getLogger("miso.conversation")


class ConversationError(RuntimeError):
    """Raised when a conversational turn cannot safely continue."""


class ConversationState(str, Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    ACKNOWLEDGING = "acknowledging"
    LISTENING = "listening"
    TRANSCRIBING = "transcribing"
    ROUTING = "routing"
    USING_TOOL = "using_tool"
    SPEAKING = "speaking"
    FOLLOW_UP = "follow_up"
    CHECKING_BACK = "checking_back"
    GOODBYE = "goodbye"
    ERROR = "error"
    STOPPED = "stopped"


_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.DISABLED: frozenset({ConversationState.STOPPED}),
    ConversationState.IDLE: frozenset(
        {
            ConversationState.ACKNOWLEDGING,
            # A talk button press is already an explicit address, so it opens
            # the microphone without a spoken acknowledgement first.
            ConversationState.LISTENING,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.ACKNOWLEDGING: frozenset(
        {
            ConversationState.LISTENING,
            ConversationState.TRANSCRIBING,
            ConversationState.ROUTING,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.LISTENING: frozenset(
        {
            ConversationState.TRANSCRIBING,
            ConversationState.ROUTING,
            ConversationState.CHECKING_BACK,
            ConversationState.GOODBYE,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.TRANSCRIBING: frozenset(
        {
            ConversationState.ROUTING,
            ConversationState.LISTENING,
            ConversationState.GOODBYE,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.ROUTING: frozenset(
        {
            ConversationState.USING_TOOL,
            ConversationState.SPEAKING,
            ConversationState.FOLLOW_UP,
            ConversationState.TRANSCRIBING,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.USING_TOOL: frozenset(
        {
            ConversationState.ROUTING,
            ConversationState.SPEAKING,
            ConversationState.FOLLOW_UP,
            ConversationState.TRANSCRIBING,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.SPEAKING: frozenset(
        {
            # Sentence-streaming TTS speaks while the model is still
            # generating, so a tool call can legitimately arrive mid-speech.
            ConversationState.USING_TOOL,
            ConversationState.ROUTING,
            ConversationState.FOLLOW_UP,
            ConversationState.TRANSCRIBING,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.FOLLOW_UP: frozenset(
        {
            ConversationState.TRANSCRIBING,
            ConversationState.ROUTING,
            ConversationState.CHECKING_BACK,
            ConversationState.GOODBYE,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.CHECKING_BACK: frozenset(
        {
            ConversationState.FOLLOW_UP,
            ConversationState.TRANSCRIBING,
            ConversationState.GOODBYE,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.GOODBYE: frozenset(
        {
            ConversationState.TRANSCRIBING,
            ConversationState.ERROR,
            ConversationState.IDLE,
            ConversationState.STOPPED,
        }
    ),
    ConversationState.ERROR: frozenset(
        {ConversationState.IDLE, ConversationState.STOPPED}
    ),
    ConversationState.STOPPED: frozenset(
        {ConversationState.IDLE, ConversationState.STOPPED}
    ),
}


# Every state in which Miso is producing audio of its own. The Pi shares a room
# with its speaker and has no acoustic echo cancellation, so the VAD hears Miso
# and reports a barge-in. Interrupting mid-answer clears the playback buffer and
# leaves only the tail audible, so all self-audio is guarded. Wake-word barge-in
# is unaffected: openWakeWord is far more selective than the VAD, so saying the
# wake phrase still interrupts.
_OUTPUT_STATES = frozenset(
    {
        ConversationState.ACKNOWLEDGING,
        ConversationState.SPEAKING,
        ConversationState.CHECKING_BACK,
        ConversationState.GOODBYE,
    }
)

# The only states in which a microphone onset means the user is addressing Miso.
# Everywhere else Miso is speaking or working, and on this hardware the VAD
# fires on its own speaker and on room noise, so treating an onset as a barge-in
# cancelled real turns mid-answer. The wake phrase stays the way to interrupt.
def _normalize_for_match(text: str) -> str:
    """Reduce text to comparable words so recogniser noise does not defeat it."""
    return " ".join(re.sub(r"[^\w\sáéíóúüñ]+", " ", text.casefold()).split())


# A transcript of Miso's own voice is never a clean copy of it: the speaker
# colours it, the room adds reverb, and whisper fills the gaps with words that
# were never spoken ("Air molecules scatter the shorter blue wavelengths" came
# back as "We'll scatter the shorter blue wavelengths"). Exact and substring
# matching both miss that, so the comparison is on shared content instead.
_ECHO_SIMILARITY = 0.6
_ECHO_MINIMUM_WORDS = 4


def _echo_overlap(candidate: str, spoken: str) -> float:
    """Share of the candidate's words that also appear in what Miso said."""
    heard = candidate.split()
    if not heard:
        return 0.0
    said = set(spoken.split())
    return sum(1 for word in heard if word in said) / len(heard)


_VOICE_ADDRESSABLE = frozenset(
    {
        ConversationState.LISTENING,
        ConversationState.FOLLOW_UP,
    }
)

# States in which there is nothing to interrupt.
_INACTIVE_STATES = frozenset(
    {
        ConversationState.IDLE,
        ConversationState.DISABLED,
        ConversationState.STOPPED,
    }
)


def strip_wake_phrase(text: str, wake_phrase: str) -> str:
    """Drop a leading wake phrase so only the request itself remains."""
    normalized = text.strip()
    phrase = re.escape(wake_phrase.strip())
    pattern = rf"^{phrase}(?=$|[\s,.:;!?-])(?:\s*[,.:;!?-]\s*|\s+)?"
    return re.sub(pattern, "", normalized, count=1, flags=re.IGNORECASE).strip()


def _monotonic_for(wall_clock: float) -> float:
    """Place a wall-clock timestamp on the monotonic clock used for latency."""
    return time.monotonic() - max(0.0, time.time() - wall_clock)


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """What a turn needs to report its own wake-to-first-audio latency."""

    turn: int
    origin: str
    origin_at: float
    conversation_id: str | None


@dataclass(frozen=True, slots=True)
class StateTransition:
    previous: ConversationState
    current: ConversationState
    reason: str
    occurred_at: float

    def as_dict(self) -> dict[str, object]:
        return {
            "previous": self.previous.value,
            "current": self.current.value,
            "reason": self.reason,
            "occurred_at": round(self.occurred_at, 3),
        }


class WakeEvents(Protocol):
    enabled: bool

    def get_event(self, timeout: float | None = None) -> WakeEvent | None: ...

    def activate(self, event: WakeEvent) -> None: ...


class TranscriptionEvents(Protocol):
    enabled: bool

    def get_result(
        self, timeout: float | None = None
    ) -> TranscriptionResult | None: ...

    def get_activity(
        self, timeout: float | None = None
    ) -> SpeechActivity | None: ...


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+")

# Piper emits nothing until it has synthesised the whole string it was given:
# measured on the Pi, first audio tracks length at roughly 65 ms per word
# (1 word 280 ms, 12 words 1223 ms, 32 words 2323 ms). Waiting for a sentence
# therefore buys silence in proportion to how long that sentence is, so a long
# opening sentence is broken at its own punctuation as well.
_CLAUSE_BOUNDARY = re.compile(r"(?<=[,;:])\s+|\s+[—–-]\s+")

# Below this a fragment is too short to sound like speech rather than a stutter.
# Above the maximum the wait is worse than the seam, so a clause break is looked
# for - only at real punctuation, since splitting mid-clause makes Piper put an
# unnatural pause where the writer never meant one. A sentence with no comma in
# it is still spoken whole. Twelve words is about 800 ms of synthesis against
# nearly four seconds of audio, so playback stays well ahead of the next
# fragment once it starts.
_MINIMUM_SEGMENT_WORDS = 4
_MAXIMUM_SEGMENT_WORDS = 12


def _split_long_clause(buffer: str) -> tuple[str, str]:
    """Split an over-long pending sentence at its last usable clause break."""
    if len(buffer.split()) <= _MAXIMUM_SEGMENT_WORDS:
        return "", buffer
    head = ""
    rest = buffer
    while True:
        match = _CLAUSE_BOUNDARY.search(rest)
        if match is None:
            break
        candidate = rest[: match.start()]
        remainder = rest[match.end() :]
        if len(candidate.split()) < _MINIMUM_SEGMENT_WORDS:
            # Too small on its own; keep it attached to what follows.
            break
        head = (head + " " + candidate).strip()
        rest = remainder
        if len(head.split()) >= _MINIMUM_SEGMENT_WORDS:
            break
    if not head or len(rest.split()) < _MINIMUM_SEGMENT_WORDS:
        return "", buffer
    return head, rest


class _SegmentSpeaker:
    """Speak completed sentences while the rest of the answer still streams.

    Synthesis of sentence N overlaps generation of sentence N+1, so the first
    audio starts as soon as the first sentence exists instead of after the
    whole answer. Segments are spoken strictly one at a time because
    SpeechManager.speak cancels any active request.
    """

    def __init__(
        self,
        manager: "ConversationManager",
        language: str,
        generation: int,
        cancel: threading.Event,
        on_first_audio: Callable[[float], None] | None = None,
    ) -> None:
        self._manager = manager
        self._language = language
        self._generation = generation
        self._cancel = cancel
        self._on_first_audio = on_first_audio
        self._reported_first_audio = False
        self._buffer = ""
        self.received = False
        self.opened_gate = False

    def feed(self, text: str) -> None:
        self.received = True
        self._buffer += text
        parts = _SENTENCE_BOUNDARY.split(self._buffer)
        if len(parts) > 1:
            self._buffer = parts[-1]
            for segment in parts[:-1]:
                self._speak(segment)
        head, self._buffer = _split_long_clause(self._buffer)
        if head:
            self._speak(head)

    def finish(self) -> None:
        remainder, self._buffer = self._buffer, ""
        self._speak(remainder)

    def close(self) -> None:
        if self.opened_gate:
            self._manager._close_echo_gate()
            self.opened_gate = False

    def _speak(self, segment: str) -> None:
        segment = segment.strip()
        if not segment:
            return
        manager = self._manager
        manager._transition_current(
            self._generation, ConversationState.SPEAKING, "speaking response"
        )
        if not self.opened_gate:
            manager._open_echo_gate()
            self.opened_gate = True
        manager._remember_spoken(segment)
        requested_at = time.monotonic()
        request_id = manager.speech.speak(segment, self._language)
        result = manager._wait_for_speech(request_id, self._cancel)
        self._note_first_audio(requested_at, result)
        if result is not None and result.status == "completed":
            return
        if self._cancel.is_set() or (
            result is not None and result.status == "cancelled"
        ):
            raise ProviderCancelled("voice turn was interrupted")
        raise ConversationError(
            "speech output timed out"
            if result is None
            else (result.error or "speech output failed")
        )

    def _note_first_audio(
        self, requested_at: float, result: SpeechResult | None
    ) -> None:
        """Report when the speaker first produced sound for this turn.

        Piper measures its own first chunk relative to the start of synthesis,
        so the request time plus that offset is the wall moment audio began.
        """
        if self._reported_first_audio or self._on_first_audio is None:
            return
        if result is None or result.first_audio_milliseconds is None:
            return
        self._reported_first_audio = True
        self._on_first_audio(
            requested_at + result.first_audio_milliseconds / 1000
        )


class ConversationManager:
    """Coordinate wake, STT, routing, tools, TTS, timeouts, and barge-in."""

    def __init__(
        self,
        *,
        enabled: bool,
        wake: WakeEvents,
        transcription: TranscriptionEvents,
        router: ProviderRouter,
        tools: ToolRegistry,
        speech: SpeechManager,
        memory: MemoryStore,
        fast_lane: FastLane | None = None,
        tool_picker: ToolPicker | None = None,
        audit_sink: AuditSink,
        latency_sink: AuditSink | None = None,
        system_prompt: str,
        wake_phrase: str,
        listen_timeout_seconds: float,
        checkback_timeout_seconds: float,
        acknowledgement: str = "Yes?",
        acknowledge_wake: bool = True,
        languages: tuple[str, ...] = ("en", "es"),
        checkback_english: str = "Anything else?",
        checkback_spanish: str = "¿Algo más?",
        goodbye_english: str = "Goodbye.",
        goodbye_spanish: str = "Hasta luego.",
        echo_guard_seconds: float = 0.6,
        echo_memory_seconds: float = 12.0,
        transition_capacity: int = 32,
        transition_listeners: tuple[Callable[[StateTransition], None], ...] = (),
        response_listeners: tuple[Callable[[str, str, bool], None], ...] = (),
        transcript_listeners: tuple[Callable[[str, str], None], ...] = (),
        capture_listeners: tuple[Callable[[str], None], ...] = (),
        error_listeners: tuple[Callable[[str], None], ...] = (),
    ) -> None:
        if listen_timeout_seconds <= 0 or checkback_timeout_seconds <= 0:
            raise ValueError("conversation timeouts must be positive")
        if not 0 <= echo_guard_seconds <= 10:
            raise ValueError("echo guard must be between 0 and 10 seconds")
        if not 0 <= echo_memory_seconds <= 120:
            raise ValueError("echo memory must be between 0 and 120 seconds")
        if not acknowledgement.strip():
            raise ValueError("conversation acknowledgement must not be empty")
        self.enabled = (
            enabled and wake.enabled and transcription.enabled and speech.enabled
        )
        self.wake = wake
        self.transcription = transcription
        self.router = router
        self.tools = tools
        self.speech = speech
        self.memory = memory
        self.fast_lane = fast_lane
        self.tool_picker = tool_picker
        self.audit_sink = audit_sink
        # Turn latency belongs beside the routing decisions it is judged
        # against, which is a different log from the tool audit trail.
        self.latency_sink = latency_sink or audit_sink
        self.system_prompt = system_prompt
        self.wake_phrase = wake_phrase.strip()
        self.listen_timeout_seconds = listen_timeout_seconds
        self.checkback_timeout_seconds = checkback_timeout_seconds
        self.echo_guard_seconds = echo_guard_seconds
        self.echo_memory_seconds = echo_memory_seconds
        self.acknowledgement = acknowledgement.strip()
        self.acknowledge_wake = acknowledge_wake
        self.languages = tuple(code.casefold() for code in languages) or ("en", "es")
        self.checkbacks = {"en": checkback_english, "es": checkback_spanish}
        self.goodbyes = {"en": goodbye_english, "es": goodbye_spanish}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._turn_thread: threading.Thread | None = None
        self._state = (
            ConversationState.IDLE if self.enabled else ConversationState.DISABLED
        )
        self._transitions: deque[StateTransition] = deque(maxlen=transition_capacity)
        self._transition_listeners = list(transition_listeners)
        self._response_listeners = list(response_listeners)
        self._transcript_listeners = list(transcript_listeners)
        self._capture_listeners = list(capture_listeners)
        self._error_listeners = list(error_listeners)
        self._conversation_id: str | None = None
        self._active_cancel: threading.Event | None = None
        self._generation = 0
        self._deadline: float | None = None
        self._checked_back = False
        self._language = "en"
        self._turns = 0
        self._interruptions = 0
        self._timeouts = 0
        self._errors = 0
        self._last_error: str | None = None
        self._ignore_activity_before = 0.0
        self._cue_speaking = False
        self._cue_gate_until = 0.0
        self._spoken_recently: list[tuple[str, float]] = []
        self._turn_origin: tuple[str, float] | None = None

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        with self._lock:
            if self._state is ConversationState.STOPPED:
                self._transition_locked(ConversationState.IDLE, "started")
        self._thread = threading.Thread(
            target=self._run, name="miso-conversation", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel_active()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._turn_thread is not None:
            self._turn_thread.join(timeout=2)
        with self._lock:
            if self._state is not ConversationState.STOPPED:
                self._transition_locked(ConversationState.STOPPED, "stopped")

    def status(self) -> dict[str, object]:
        with self._lock:
            latest = self._transitions[-1] if self._transitions else None
            return {
                "enabled": self.enabled,
                "state": self._state.value,
                "conversation_id": self._conversation_id,
                "language": self._language,
                "turns": self._turns,
                "interruptions": self._interruptions,
                "timeouts": self._timeouts,
                "errors": self._errors,
                "last_error": self._last_error,
                "latest_transition": None if latest is None else latest.as_dict(),
            }

    def transition(self, target: ConversationState, reason: str) -> None:
        """Apply a state transition, rejecting transitions outside the graph."""
        with self._lock:
            self._transition_locked(target, reason)

    def add_transition_listener(
        self, listener: Callable[[StateTransition], None]
    ) -> None:
        with self._lock:
            self._transition_listeners.append(listener)

    def add_response_listener(self, listener: Callable[[str, str, bool], None]) -> None:
        with self._lock:
            self._response_listeners.append(listener)

    def add_transcript_listener(self, listener: Callable[[str, str], None]) -> None:
        with self._lock:
            self._transcript_listeners.append(listener)

    def add_capture_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._capture_listeners.append(listener)

    def add_error_listener(self, listener: Callable[[str], None]) -> None:
        with self._lock:
            self._error_listeners.append(listener)

    def _publish_capture(self, state: str) -> None:
        with self._lock:
            listeners = tuple(self._capture_listeners)
        for listener in listeners:
            try:
                listener(state)
            except Exception:
                LOGGER.exception("conversation capture listener failed")

    def _publish_transcript(self, text: str, language: str) -> None:
        with self._lock:
            listeners = tuple(self._transcript_listeners)
        for listener in listeners:
            try:
                listener(text, language)
            except Exception:
                LOGGER.exception("conversation transcript listener failed")

    def _publish_error(self, error: str) -> None:
        with self._lock:
            listeners = tuple(self._error_listeners)
        for listener in listeners:
            try:
                listener(error)
            except Exception:
                LOGGER.exception("conversation error listener failed")

    def _publish_response(self, text: str, language: str, final: bool = True) -> None:
        if final:
            self._remember_spoken(text)
        with self._lock:
            listeners = tuple(self._response_listeners)
        for listener in listeners:
            try:
                listener(text, language, final)
            except Exception:
                LOGGER.exception("conversation response listener failed")

    def _open_echo_gate(self) -> None:
        """Ignore the microphone while Miso's own audio is on the speaker."""
        with self._lock:
            self._cue_speaking = True
            self._cue_gate_until = time.time() + self.echo_guard_seconds

    def _close_echo_gate(self) -> None:
        with self._lock:
            self._cue_speaking = False
            self._cue_gate_until = time.time() + self.echo_guard_seconds

    def _echo_gated_locked(self, occurred_at: float) -> bool:
        return self._cue_speaking or occurred_at <= self._cue_gate_until

    def _remember_spoken(self, text: str) -> None:
        normalized = _normalize_for_match(text)
        if not normalized:
            return
        with self._lock:
            self._spoken_recently.append((normalized, time.monotonic()))

    def _is_own_echo(self, text: str) -> bool:
        """True when a transcript is Miso hearing itself through the speaker.

        Comparing against what was actually spoken cannot mis-credit the wrong
        utterance the way a bare counter could, and it needs no timestamp on the
        transcript, which the recogniser does not provide.
        """
        candidate = _normalize_for_match(text)
        if not candidate:
            return False
        horizon = time.monotonic() - self.echo_memory_seconds
        with self._lock:
            self._spoken_recently = [
                item for item in self._spoken_recently if item[1] >= horizon
            ]
            recent = [spoken for spoken, _ in self._spoken_recently]
        for spoken in recent:
            if candidate == spoken or candidate in spoken or spoken in candidate:
                return True
            # Short transcripts stay on exact matching: "yes" or "the timer"
            # legitimately repeats words Miso just used, and discarding those
            # would swallow real follow-ups.
            if len(candidate.split()) < _ECHO_MINIMUM_WORDS:
                continue
            if (
                _echo_overlap(candidate, spoken) >= _ECHO_SIMILARITY
                or SequenceMatcher(None, candidate, spoken).ratio() >= _ECHO_SIMILARITY
            ):
                return True
        return False

    def _transition_locked(self, target: ConversationState, reason: str) -> None:
        previous = self._state
        if target is previous:
            return
        if target not in _TRANSITIONS[previous]:
            raise ConversationError(
                f"invalid conversation transition: {previous.value} -> {target.value}"
            )
        transition = StateTransition(previous, target, reason[:120], time.time())
        self._state = target
        self._transitions.append(transition)
        self.audit_sink.record(
            audit_event(
                "conversation_transition",
                **transition.as_dict(),
                actor=VOICE_ACTOR.actor_id,
                actor_source=VOICE_ACTOR.source,
            )
        )
        for listener in tuple(self._transition_listeners):
            try:
                listener(transition)
            except Exception:
                LOGGER.exception(
                    "conversation transition listener failed for %s", target.value
                )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                wake_event = self.wake.get_event(timeout=0.05)
                if wake_event is not None:
                    self._handle_wake(wake_event)
                activity = self.transcription.get_activity(timeout=0)
                while activity is not None:
                    self._handle_activity(activity)
                    activity = self.transcription.get_activity(timeout=0)
                result = self.transcription.get_result(timeout=0)
                while result is not None:
                    self._handle_transcription(result)
                    result = self.transcription.get_result(timeout=0)
                self._handle_timeout()
            except Exception as error:
                self._recover(error)
                self._stop.wait(0.05)

    def interrupt(self, reason: str) -> bool:
        """Cancel any turn and its playback, exactly as a wake interruption does.

        Returns False when nothing was in flight, so a caller such as the stop
        button can record that the press was a no-op rather than a cancellation.
        """
        with self._lock:
            if self._state in _INACTIVE_STATES:
                return False
        self._cancel_active()
        with self._lock:
            self._interruptions += 1
            if self._state is not ConversationState.IDLE:
                self._transition_locked(ConversationState.IDLE, reason)
            self._conversation_id = None
            self._checked_back = False
            self._deadline = None
        return True

    def _handle_wake(self, event: WakeEvent) -> None:
        with self._lock:
            active = self._state not in _INACTIVE_STATES
        if active:
            self._cancel_active()
            with self._lock:
                self._interruptions += 1
                if self._state is not ConversationState.IDLE:
                    self._transition_locked(ConversationState.IDLE, "wake interruption")
        with self._lock:
            if self._state is not ConversationState.IDLE:
                return
            self._conversation_id = self.memory.create_conversation(
                actor=VOICE_ACTOR, visibility="shared"
            )
            self._language = "en"
            self._checked_back = False
            self._deadline = None
            self._last_error = None
            self._ignore_activity_before = event.detected_at
            button = event.source == WAKE_SOURCE_BUTTON
            if button or not self.acknowledge_wake:
                # A button press is already an unambiguous address, and when the
                # spoken acknowledgement is off the wake phrase is treated the
                # same way: someone saying "Miso, set a timer" in one breath is
                # still talking while "Yes?" would be playing, so the tail of
                # the sentence lands in the microphone mixed with Miso's own
                # voice. Opening the microphone immediately keeps that audio
                # clean and the listening cue on the display carries the
                # feedback the sound used to.
                origin = "button" if button else "wake"
                self._turn_origin = (origin, _monotonic_for(event.detected_at))
                self._deadline = time.monotonic() + self.listen_timeout_seconds
                self._transition_locked(
                    ConversationState.LISTENING,
                    "button talk" if button else "wake detected",
                )
                return
            self._turn_origin = ("wake", _monotonic_for(event.detected_at))
            self._transition_locked(ConversationState.ACKNOWLEDGING, "wake detected")
        self._start_cue(
            self.acknowledgement,
            "en",
            ConversationState.LISTENING,
            self.listen_timeout_seconds,
            "acknowledgement completed",
        )

    def _handle_activity(self, activity: SpeechActivity) -> None:
        if activity.kind == "discarded":
            discarded = False
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._active_cancel = None
                    self._deadline = time.monotonic() + self.listen_timeout_seconds
                    self._transition_locked(
                        ConversationState.LISTENING,
                        "short utterance discarded",
                    )
                    discarded = True
            if discarded:
                self._publish_capture("discarded")
            return
        if activity.kind == "ended":
            # The utterance is now with the recogniser, which is the slow half
            # of the wait, so the cue changes rather than disappearing.
            with self._lock:
                waiting = self._state is ConversationState.TRANSCRIBING
            if waiting:
                self._publish_capture("transcribing")
            return
        if activity.kind != "started":
            return
        capturing = False
        with self._lock:
            if activity.occurred_at <= self._ignore_activity_before:
                return
            if self._echo_gated_locked(activity.occurred_at):
                # Miso's own audio is still on the speaker, or just left it.
                # There is no echo cancellation between the two, so an onset
                # here is the tail of the answer being recorded as the next
                # question - which is how a reply once fed itself back in as a
                # follow-up three turns running.
                LOGGER.debug("ignored microphone onset inside the echo gate")
                return
            state = self._state
            if state not in _VOICE_ADDRESSABLE:
                # Miso is either speaking or working on a turn. A microphone
                # onset here is its own echo, room noise, or an impatient
                # repeat, and destroying the in-flight answer for any of those
                # leaves the request permanently unanswered.
                return
            if self._state is state:
                self._deadline = None
                if self._turn_origin is None:
                    self._turn_origin = (
                        "follow_up",
                        _monotonic_for(activity.occurred_at),
                    )
                self._transition_locked(
                    ConversationState.TRANSCRIBING, "speech detected"
                )
                capturing = True
        if capturing:
            self._publish_capture("capturing")

    def _handle_transcription(self, result: TranscriptionResult) -> None:
        with self._lock:
            state = self._state
        if state is ConversationState.IDLE:
            if self._starts_with_wake_phrase(result.text):
                LOGGER.info("wake phrase confirmed by transcription fallback")
                self.wake.activate(
                    WakeEvent(
                        self.wake_phrase,
                        result.confidence or 0.0,
                        time.time(),
                        source="transcription",
                    )
                )
            return
        with self._lock:
            if state not in {
                ConversationState.ACKNOWLEDGING,
                ConversationState.LISTENING,
                ConversationState.FOLLOW_UP,
                ConversationState.TRANSCRIBING,
                ConversationState.ROUTING,
                ConversationState.USING_TOOL,
                ConversationState.SPEAKING,
                ConversationState.CHECKING_BACK,
                ConversationState.GOODBYE,
            }:
                return
        if state not in {
            ConversationState.LISTENING,
            ConversationState.FOLLOW_UP,
            ConversationState.TRANSCRIBING,
        }:
            # Miso is speaking or working on a turn. Cancelling here cleared the
            # playback buffer mid-phrase and left only the tail of the answer
            # audible. The wake phrase is the only interrupt.
            LOGGER.debug("ignored transcript received while %s", state.value)
            return
        text = self._without_wake_phrase(result.text)
        if not text:
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._transition_locked(
                        ConversationState.LISTENING, "wake-only transcript ignored"
                    )
                self._deadline = time.monotonic() + self.listen_timeout_seconds
            self._publish_capture("discarded")
            return
        detected = (result.model_language or "").casefold()
        if detected and detected not in self.languages and result.language != "mixed":
            # whisper auto-detects across a hundred languages; a verdict outside
            # the household's own two means the audio was noise or a language
            # Miso cannot answer in, and the transcript that came with it is not
            # worth acting on.
            LOGGER.info("dropped transcript detected as %s", detected)
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._transition_locked(
                        ConversationState.LISTENING, "unsupported language ignored"
                    )
                self._deadline = time.monotonic() + self.listen_timeout_seconds
            self._publish_capture("discarded")
            return
        language = (
            "es"
            if result.language == "es" or result.model_language == "es"
            else "en"
        )
        with self._lock:
            self._language = language
        if self._is_own_echo(text):
            LOGGER.debug("dropped transcript matching Miso's own recent speech")
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._transition_locked(
                        ConversationState.LISTENING, "own speech ignored"
                    )
                self._deadline = time.monotonic() + self.listen_timeout_seconds
            self._publish_capture("discarded")
            return
        if self._is_goodbye(text):
            with self._lock:
                self._transition_locked(ConversationState.GOODBYE, "goodbye requested")
            self._start_cue(
                self.goodbyes[language],
                language,
                ConversationState.IDLE,
                None,
                "goodbye completed",
            )
            return
        self._start_turn(text, language, result)

    def _start_turn(
        self, text: str, language: str, transcription: TranscriptionResult
    ) -> None:
        cancel = threading.Event()
        with self._lock:
            if self._state not in {
                ConversationState.ACKNOWLEDGING,
                ConversationState.LISTENING,
                ConversationState.FOLLOW_UP,
                ConversationState.TRANSCRIBING,
            }:
                return
            self._generation += 1
            generation = self._generation
            self._active_cancel = cancel
            self._deadline = None
            self._transition_locked(
                ConversationState.ROUTING, "transcription completed"
            )
            self._turns += 1
            origin, origin_at = self._turn_origin or ("turn", time.monotonic())
            self._turn_origin = None
            context = _TurnContext(
                self._turns, origin, origin_at, self._conversation_id
            )
        self._publish_transcript(text, language)
        self._turn_thread = threading.Thread(
            target=self._execute_turn,
            args=(generation, cancel, text, language, transcription, context),
            name="miso-conversation-turn",
            daemon=True,
        )
        self._turn_thread.start()

    def _execute_turn(
        self,
        generation: int,
        cancel: threading.Event,
        text: str,
        language: str,
        transcription: TranscriptionResult,
        context: _TurnContext,
    ) -> None:
        lane = "model"
        speaker = _SegmentSpeaker(
            self,
            language,
            generation,
            cancel,
            on_first_audio=lambda at: self._record_first_audio(
                context, lane, language, transcription, at
            ),
        )
        try:
            with self._lock:
                conversation_id = self._conversation_id
            if conversation_id is None:
                raise ConversationError("voice conversation has no conversation ID")
            self.memory.append_event(
                conversation_id,
                kind="message",
                role="user",
                content=text,
                payload={
                    "source": "voice",
                    "language": language,
                    "confidence": transcription.confidence,
                },
                actor=VOICE_ACTOR,
            )
            fast_reply = None
            if self.fast_lane is not None:
                fast_reply = self.fast_lane.try_handle(
                    text, language, cancel_event=cancel, actor=VOICE_ACTOR
                )
            if fast_reply is None and self.tool_picker is not None:
                fast_reply = self.tool_picker.try_handle(
                    text, language, cancel_event=cancel, actor=VOICE_ACTOR
                )
            used_tool = False
            partial: list[str] = []
            if fast_reply is not None:
                lane = "fast"
                self._transition_current(
                    generation,
                    ConversationState.USING_TOOL,
                    f"intent matched: {fast_reply.intent}",
                )
                self.memory.append_event(
                    conversation_id,
                    kind="tool",
                    role="assistant",
                    content=fast_reply.tool,
                    payload=fast_reply.result.as_dict(),
                    actor=VOICE_ACTOR,
                )
                used_tool = True
                spoken = fast_reply.spoken
            else:
                history = self.memory.events(
                    conversation_id, limit=40, actor=VOICE_ACTOR
                )
                request = ChatRequest(
                    messages=(
                        {"role": "system", "content": self.system_prompt},
                        *(
                            {"role": event.role, "content": event.content}
                            for event in history
                            if event.kind == "message"
                            and event.role in {"user", "assistant"}
                            and event.content
                        ),
                    ),
                    tools=self.tools.schemas(),
                )
                response: list[str] = []
                tool_summaries: list[str] = []
                for chunk in self._stream_turn(
                    request, cancel, generation, response, partial
                ):
                    if chunk.text:
                        response.append(chunk.text)
                        self._publish_response(
                            "".join(response).strip(), language, False
                        )
                        speaker.feed(chunk.text)
                    if chunk.tool_call is not None:
                        name = chunk.tool_call.get("name")
                        arguments = chunk.tool_call.get("arguments")
                        if not isinstance(name, str) or not isinstance(arguments, dict):
                            raise RoutingError("provider returned invalid tool call")
                        self._transition_current(
                            generation, ConversationState.USING_TOOL, "tool requested"
                        )
                        result = self.tools.invoke(
                            name, arguments, cancel_event=cancel, actor=VOICE_ACTOR
                        )
                        self.memory.append_event(
                            conversation_id,
                            kind="tool",
                            role="assistant",
                            content=name,
                            payload=result.as_dict(),
                            actor=VOICE_ACTOR,
                        )
                        used_tool = True
                        if not result.ok and not cancel.is_set():
                            raise ConversationError(result.error or "tool invocation failed")
                        if result.summary is not None:
                            tool_summaries.append(result.summary)
                        self._transition_current(
                            generation, ConversationState.ROUTING, "tool completed"
                        )
                if cancel.is_set() or not self._is_current(generation):
                    return
                spoken = "".join(response).strip()
                if not spoken and tool_summaries:
                    spoken = " ".join(tool_summaries)
                if not spoken:
                    spoken = "Listo." if language == "es" else "Done."
            self.memory.append_event(
                conversation_id,
                kind="message",
                role="assistant",
                content=spoken,
                payload={
                    "source": "voice",
                    "used_tool": used_tool,
                    **({"fast_intent": fast_reply.intent} if fast_reply else {}),
                    **({"partial": partial[0]} if partial else {}),
                },
                actor=VOICE_ACTOR,
            )
            self._publish_response(spoken, language)
            if not speaker.received:
                speaker.feed(spoken)
            speaker.finish()
            if not cancel.is_set() and self._is_current(generation):
                with self._lock:
                    self._checked_back = False
                    self._deadline = time.monotonic() + self.listen_timeout_seconds
                    self._active_cancel = None
                    self._transition_locked(
                        ConversationState.FOLLOW_UP, "response completed"
                    )
        except ProviderCancelled:
            return
        except Exception as error:
            if not cancel.is_set() and self._is_current(generation):
                self._recover(error)
        finally:
            speaker.close()

    def _record_first_audio(
        self,
        context: _TurnContext,
        lane: str,
        language: str,
        transcription: TranscriptionResult,
        first_audio_at: float,
    ) -> None:
        """Record how long the user waited between speaking and hearing audio.

        This is the number a listener actually experiences, and no existing
        audit entry covers it: routing latency omits transcription and the
        fast lane skips routing altogether.
        """
        try:
            self.latency_sink.record(
                audit_event(
                    "turn_first_audio",
                    conversation_id=context.conversation_id,
                    turn=context.turn,
                    origin=context.origin,
                    lane=lane,
                    language=language,
                    status="completed",
                    latency_ms=max(
                        0, round((first_audio_at - context.origin_at) * 1000)
                    ),
                    transcription_ms=transcription.inference_milliseconds,
                    actor=VOICE_ACTOR.actor_id,
                    actor_source=VOICE_ACTOR.source,
                )
            )
        except Exception:
            # A metric must never take the answer down with it.
            LOGGER.exception("could not record turn first-audio latency")

    def _stream_turn(
        self,
        request: ChatRequest,
        cancel: threading.Event,
        generation: int,
        produced: list[str],
        failure: list[str],
    ):
        """Yield routed chunks, tolerating a provider that dies mid-answer.

        A small local model can stall or drop the connection after emitting
        usable text. Re-raising there discards the whole answer and Miso goes
        silent, so partial text is kept and the reason recorded instead.
        """
        try:
            for chunk in self.router.stream(request, cancel, actor=VOICE_ACTOR):
                if cancel.is_set() or not self._is_current(generation):
                    raise ProviderCancelled("voice turn was interrupted")
                yield chunk
        except RoutingError as error:
            if not produced or cancel.is_set() or not self._is_current(generation):
                raise
            failure.append(str(error)[:200])
            LOGGER.warning("speaking partial voice answer: %s", error)

    def _start_cue(
        self,
        text: str,
        language: str,
        target: ConversationState,
        timeout: float | None,
        reason: str,
    ) -> None:
        cancel = threading.Event()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._active_cancel = cancel
        self._turn_thread = threading.Thread(
            target=self._execute_cue,
            args=(generation, cancel, text, language, target, timeout, reason),
            name="miso-conversation-cue",
            daemon=True,
        )
        self._turn_thread.start()

    def _execute_cue(
        self,
        generation: int,
        cancel: threading.Event,
        text: str,
        language: str,
        target: ConversationState,
        timeout: float | None,
        reason: str,
    ) -> None:
        try:
            if cancel.is_set() or not self._is_current(generation):
                return
            self._publish_response(text, language)
            self._open_echo_gate()
            try:
                request_id = self.speech.speak(text, language)
                result = self._wait_for_speech(request_id, cancel)
            finally:
                self._close_echo_gate()
            if cancel.is_set() or not self._is_current(generation):
                return
            if result is None or result.status != "completed":
                raise ConversationError(
                    "speech cue timed out"
                    if result is None
                    else (result.error or result.status)
                )
            with self._lock:
                self._active_cancel = None
                self._deadline = (
                    None if timeout is None else time.monotonic() + timeout
                )
                self._transition_locked(target, reason)
                if target is ConversationState.IDLE:
                    self._conversation_id = None
                    self._checked_back = False
        except Exception as error:
            if not cancel.is_set() and self._is_current(generation):
                self._recover(error)

    def _wait_for_speech(
        self, request_id: str, cancel: threading.Event
    ) -> SpeechResult | None:
        while not cancel.is_set() and not self._stop.is_set():
            result = self.speech.wait(request_id, 0.05)
            if result is not None:
                return result
        self.speech.cancel(request_id)
        return self.speech.wait(request_id, 1)

    def _handle_timeout(self) -> None:
        with self._lock:
            if self._deadline is None or time.monotonic() < self._deadline:
                return
            state = self._state
            self._deadline = None
            self._timeouts += 1
            checked_back = self._checked_back
            language = self._language
            if state not in {ConversationState.LISTENING, ConversationState.FOLLOW_UP}:
                return
            if not checked_back:
                self._checked_back = True
                self._transition_locked(
                    ConversationState.CHECKING_BACK, "listening timeout"
                )
            else:
                self._transition_locked(ConversationState.GOODBYE, "follow-up timeout")
        if not checked_back:
            self._start_cue(
                self.checkbacks[language],
                language,
                ConversationState.FOLLOW_UP,
                self.checkback_timeout_seconds,
                "check-back completed",
            )
        else:
            self._start_cue(
                self.goodbyes[language],
                language,
                ConversationState.IDLE,
                None,
                "timeout goodbye completed",
            )

    def _cancel_active(self) -> None:
        with self._lock:
            cancel = self._active_cancel
        if cancel is not None:
            cancel.set()
        self.speech.cancel()

    def _recover(self, error: Exception) -> None:
        self._publish_error(str(error)[:200])
        with self._lock:
            self._errors += 1
            self._last_error = str(error)[:200]
            self._deadline = None
            self._active_cancel = None
            self._turn_origin = None
            if self._state not in {
                ConversationState.ERROR,
                ConversationState.IDLE,
                ConversationState.STOPPED,
            }:
                self._transition_locked(ConversationState.ERROR, "turn failed")
            if self._state is ConversationState.ERROR:
                self._transition_locked(ConversationState.IDLE, "recovered safely")
            self._conversation_id = None

    def _transition_current(
        self, generation: int, target: ConversationState, reason: str
    ) -> None:
        with self._lock:
            if generation != self._generation:
                raise ProviderCancelled("voice turn was superseded")
            self._transition_locked(target, reason)

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _without_wake_phrase(self, text: str) -> str:
        return strip_wake_phrase(text, self.wake_phrase)

    def _starts_with_wake_phrase(self, text: str) -> bool:
        normalized = text.strip()
        return bool(normalized) and self._without_wake_phrase(normalized) != normalized

    @staticmethod
    def _is_goodbye(text: str) -> bool:
        normalized = re.sub(r"[^\wáéíóúüñ]+", " ", text.casefold()).strip()
        return normalized in {
            "bye",
            "goodbye",
            "good bye",
            "that is all",
            "that s all",
            "adios",
            "adiós",
            "hasta luego",
            "eso es todo",
            "nada más",
            "nada mas",
        }
