"""Explicit state machine for offline conversational voice turns."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from miso.memory import MemoryStore
from miso.identity import VOICE_ACTOR
from miso.providers import ChatRequest, ProviderCancelled
from miso.routing import ProviderRouter, RoutingError
from miso.speech import SpeechManager, SpeechResult
from miso.tools import ToolRegistry
from miso.tools.audit import AuditSink, audit_event
from miso.transcription import SpeechActivity, TranscriptionResult
from miso.wake import WakeEvent


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
        {ConversationState.ACKNOWLEDGING, ConversationState.STOPPED}
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
_VOICE_ADDRESSABLE = frozenset(
    {
        ConversationState.LISTENING,
        ConversationState.FOLLOW_UP,
    }
)


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
        audit_sink: AuditSink,
        system_prompt: str,
        wake_phrase: str,
        listen_timeout_seconds: float,
        checkback_timeout_seconds: float,
        acknowledgement: str = "Yes?",
        checkback_english: str = "Anything else?",
        checkback_spanish: str = "¿Algo más?",
        goodbye_english: str = "Goodbye.",
        goodbye_spanish: str = "Hasta luego.",
        echo_guard_seconds: float = 0.6,
        transition_capacity: int = 32,
        transition_listeners: tuple[Callable[[StateTransition], None], ...] = (),
        response_listeners: tuple[Callable[[str, str, bool], None], ...] = (),
    ) -> None:
        if listen_timeout_seconds <= 0 or checkback_timeout_seconds <= 0:
            raise ValueError("conversation timeouts must be positive")
        if not 0 <= echo_guard_seconds <= 10:
            raise ValueError("echo guard must be between 0 and 10 seconds")
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
        self.audit_sink = audit_sink
        self.system_prompt = system_prompt
        self.wake_phrase = wake_phrase.strip()
        self.listen_timeout_seconds = listen_timeout_seconds
        self.checkback_timeout_seconds = checkback_timeout_seconds
        self.echo_guard_seconds = echo_guard_seconds
        self.acknowledgement = acknowledgement.strip()
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
        self._suppressed_utterances: list[float] = []

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

    def _publish_response(self, text: str, language: str, final: bool = True) -> None:
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

    def _suppress_utterance_locked(self) -> None:
        # Each suppressed onset consumes the transcript it will produce. The
        # deadline lets the count self-heal if transcription drops an utterance.
        self._prune_suppressed_locked()
        self._suppressed_utterances.append(
            time.monotonic() + max(5.0, self.echo_guard_seconds * 10)
        )

    def _prune_suppressed_locked(self) -> None:
        now = time.monotonic()
        self._suppressed_utterances = [
            deadline for deadline in self._suppressed_utterances if deadline > now
        ]

    def _consume_suppressed(self) -> bool:
        with self._lock:
            self._prune_suppressed_locked()
            if not self._suppressed_utterances:
                return False
            self._suppressed_utterances.pop(0)
            return True

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

    def _handle_wake(self, event: WakeEvent) -> None:
        with self._lock:
            active = self._state not in {
                ConversationState.IDLE,
                ConversationState.DISABLED,
                ConversationState.STOPPED,
            }
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
            if self._consume_suppressed():
                return
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._active_cancel = None
                    self._deadline = time.monotonic() + self.listen_timeout_seconds
                    self._transition_locked(
                        ConversationState.LISTENING,
                        "short utterance discarded",
                    )
            return
        if activity.kind != "started":
            return
        with self._lock:
            if activity.occurred_at <= self._ignore_activity_before:
                return
            state = self._state
            if state not in _VOICE_ADDRESSABLE:
                # Miso is either speaking or working on a turn. A microphone
                # onset here is its own echo, room noise, or an impatient
                # repeat, and destroying the in-flight answer for any of those
                # leaves the request permanently unanswered. Suppress the
                # transcript this onset will produce so it is never routed as a
                # fresh request either.
                self._suppress_utterance_locked()
                return
            if self._state is state:
                self._deadline = None
                self._transition_locked(
                    ConversationState.TRANSCRIBING, "speech detected"
                )

    def _handle_transcription(self, result: TranscriptionResult) -> None:
        if self._consume_suppressed():
            LOGGER.debug("dropped transcript captured from Miso's own cue audio")
            return
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
            self._cancel_active()
            with self._lock:
                if self._state is state:
                    self._interruptions += 1
                    self._deadline = None
                    self._transition_locked(
                        ConversationState.TRANSCRIBING,
                        "transcription interrupted output",
                    )
        text = self._without_wake_phrase(result.text)
        if not text:
            with self._lock:
                if self._state is ConversationState.TRANSCRIBING:
                    self._transition_locked(
                        ConversationState.LISTENING, "wake-only transcript ignored"
                    )
                self._deadline = time.monotonic() + self.listen_timeout_seconds
            return
        language = (
            "es"
            if result.language == "es" or result.model_language == "es"
            else "en"
        )
        with self._lock:
            self._language = language
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
        self._turn_thread = threading.Thread(
            target=self._execute_turn,
            args=(generation, cancel, text, language, transcription),
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
    ) -> None:
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
            used_tool = False
            partial: list[str] = []
            for chunk in self._stream_turn(
                request, cancel, generation, response, partial
            ):
                if chunk.text:
                    response.append(chunk.text)
                    self._publish_response(
                        "".join(response).strip(), language, False
                    )
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
                    **({"partial": partial[0]} if partial else {}),
                },
                actor=VOICE_ACTOR,
            )
            self._transition_current(
                generation, ConversationState.SPEAKING, "response ready"
            )
            self._publish_response(spoken, language)
            self._open_echo_gate()
            try:
                request_id = self.speech.speak(spoken, language)
                result = self._wait_for_speech(request_id, cancel)
            finally:
                self._close_echo_gate()
            if (
                result is not None
                and result.status == "completed"
                and self._is_current(generation)
            ):
                with self._lock:
                    self._checked_back = False
                    self._deadline = time.monotonic() + self.listen_timeout_seconds
                    self._active_cancel = None
                    self._transition_locked(
                        ConversationState.FOLLOW_UP, "response completed"
                    )
            elif (
                result is not None
                and result.status == "error"
                and not cancel.is_set()
            ):
                raise ConversationError(result.error or "speech output failed")
        except ProviderCancelled:
            return
        except Exception as error:
            if not cancel.is_set() and self._is_current(generation):
                self._recover(error)

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
        with self._lock:
            self._errors += 1
            self._last_error = str(error)[:200]
            self._deadline = None
            self._active_cancel = None
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
        normalized = text.strip()
        phrase = re.escape(self.wake_phrase)
        pattern = rf"^{phrase}(?=$|[\s,.:;!?-])(?:\s*[,.:;!?-]\s*|\s+)?"
        return re.sub(pattern, "", normalized, count=1, flags=re.IGNORECASE).strip()

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
