"""Physical BMO buttons: instant talk and immediate stop, over the Pi GPIO.

gpiozero is used rather than libgpiod because it already ships with Raspberry Pi
OS, gives debounce, hold detection, and both edges as callbacks instead of a
hand-rolled event loop over line-request file descriptors, and provides a mock
pin factory so the routing below is testable without hardware. Nothing here may
raise on a machine with no GPIO: the feature disables itself instead.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from functools import partial
from typing import Callable, Protocol

from miso.identity import VOICE_ACTOR
from miso.tools.audit import AuditSink, audit_event
from miso.wake import WAKE_SOURCE_BUTTON, WakeEvent

LOGGER = logging.getLogger("miso.buttons")

TALK = "talk"
STOP = "stop"
BUTTONS = (TALK, STOP)


class WakePublisher(Protocol):
    enabled: bool

    def activate(self, event: WakeEvent) -> None: ...


class TurnInterrupter(Protocol):
    def interrupt(self, reason: str) -> bool: ...


class ButtonRouter:
    """Translate button edges into wake activations and turn interruptions.

    Debounce is applied here as well as on the pin so that any edge source, a
    mock factory included, cannot double-fire a turn: a stray second press on
    the talk button would otherwise destroy the turn the first one just opened.
    """

    def __init__(
        self,
        *,
        wake: WakePublisher,
        conversation: TurnInterrupter,
        audit_sink: AuditSink,
        wake_phrase: str,
        debounce_seconds: float,
        hold_seconds: float,
        on_talk_hold: Callable[[float], None] | None = None,
        on_talk_release: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if debounce_seconds < 0:
            raise ValueError("button debounce must not be negative")
        if hold_seconds <= 0:
            raise ValueError("button hold must be positive")
        if not wake_phrase.strip():
            raise ValueError("button wake phrase must not be empty")
        self.wake = wake
        self.conversation = conversation
        self.audit_sink = audit_sink
        self.wake_phrase = wake_phrase.strip()
        self.debounce_seconds = debounce_seconds
        self.hold_seconds = hold_seconds
        self.on_talk_hold = on_talk_hold
        self.on_talk_release = on_talk_release
        self._clock = clock
        self._lock = threading.Lock()
        self._last_press = {name: -math.inf for name in BUTTONS}
        self._pressed_at: dict[str, float] = {}
        self._presses = {name: 0 for name in BUTTONS}
        self._bounces = 0
        self._last_press_at: float | None = None

    def press(self, name: str) -> bool:
        """Act on a press, returning False when it is rejected as a bounce."""
        self._require(name)
        now = self._clock()
        with self._lock:
            if now - self._last_press[name] < self.debounce_seconds:
                self._bounces += 1
                return False
            self._last_press[name] = now
            self._pressed_at[name] = now
            self._presses[name] += 1
            self._last_press_at = time.time()
        if name == TALK:
            self._talk()
        else:
            self._stop()
        return True

    def hold(self, name: str) -> None:
        """Act on a press still held past the hold threshold."""
        self._require(name)
        with self._lock:
            pressed_at = self._pressed_at.get(name)
        if pressed_at is None:
            return
        held = max(0.0, self._clock() - pressed_at)
        self._record("button_hold", button=name, held_seconds=round(held, 3))
        if name == TALK and self.on_talk_hold is not None:
            self.on_talk_hold(held)

    def release(self, name: str) -> float | None:
        """Close a press, returning how long it was held, if it was tracked."""
        self._require(name)
        with self._lock:
            pressed_at = self._pressed_at.pop(name, None)
        if pressed_at is None:
            return None
        held = max(0.0, self._clock() - pressed_at)
        if name == TALK and self.on_talk_release is not None:
            self.on_talk_release(held)
        return held

    def counters(self) -> dict[str, object]:
        with self._lock:
            return {
                "talk_presses": self._presses[TALK],
                "stop_presses": self._presses[STOP],
                "bounces": self._bounces,
                "last_press_at": (
                    None
                    if self._last_press_at is None
                    else round(self._last_press_at, 3)
                ),
            }

    def _talk(self) -> None:
        self.wake.activate(
            WakeEvent(
                self.wake_phrase,
                1.0,
                time.time(),
                source=WAKE_SOURCE_BUTTON,
            )
        )
        self._record("button_press", button=TALK, action="talk")

    def _stop(self) -> None:
        interrupted = self.conversation.interrupt("button stop")
        self._record(
            "button_press", button=STOP, action="stop", interrupted=interrupted
        )

    def _record(self, event: str, **fields: object) -> None:
        # The physical control must keep working even when the audit sink
        # cannot, so the action has already happened by the time this runs.
        try:
            self.audit_sink.record(
                audit_event(
                    event,
                    actor=VOICE_ACTOR.actor_id,
                    actor_source=WAKE_SOURCE_BUTTON,
                    **fields,
                )
            )
        except Exception:
            LOGGER.exception("could not audit %s", event)

    @staticmethod
    def _require(name: str) -> None:
        if name not in BUTTONS:
            raise ValueError(f"unknown button: {name}")


class ButtonManager:
    """Bind the router to GPIO pins, degrading to disabled without hardware."""

    def __init__(
        self,
        *,
        enabled: bool,
        router: ButtonRouter,
        talk_pin: int,
        stop_pin: int,
        pull_up: bool,
        bounce_seconds: float,
        hold_seconds: float,
        pin_factory: object | None = None,
    ) -> None:
        if talk_pin == stop_pin:
            raise ValueError("button pins must differ")
        self.enabled = enabled
        self.router = router
        self.talk_pin = talk_pin
        self.stop_pin = stop_pin
        self.pull_up = pull_up
        self.bounce_seconds = bounce_seconds
        self.hold_seconds = hold_seconds
        self.pin_factory = pin_factory
        self._lock = threading.Lock()
        self._devices: dict[str, object] = {}
        self._state = "disabled" if not enabled else "stopped"
        self._last_error: str | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._devices:
                return
        try:
            from gpiozero import Button
        except ImportError as error:
            self._unavailable("gpiozero is not installed", error)
            return
        devices: dict[str, object] = {}
        try:
            for name, pin in ((TALK, self.talk_pin), (STOP, self.stop_pin)):
                button = Button(
                    pin,
                    pull_up=self.pull_up,
                    bounce_time=self.bounce_seconds or None,
                    hold_time=self.hold_seconds,
                    pin_factory=self.pin_factory,
                )
                button.when_pressed = partial(self._edge, self.router.press, name)
                button.when_held = partial(self._edge, self.router.hold, name)
                button.when_released = partial(self._edge, self.router.release, name)
                devices[name] = button
        except Exception as error:
            # gpiozero raises its own exception tree plus OSError for a missing
            # or busy chip, and it cannot be named before the import succeeds.
            for device in devices.values():
                self._close(device)
            self._unavailable("GPIO pins are unavailable", error)
            return
        with self._lock:
            self._devices = devices
            self._state = "listening"
            self._last_error = None
        LOGGER.info(
            "buttons listening on BCM %s (talk) and BCM %s (stop)",
            self.talk_pin,
            self.stop_pin,
        )

    def stop(self) -> None:
        with self._lock:
            devices, self._devices = self._devices, {}
            if self._state == "listening":
                self._state = "stopped"
        for device in devices.values():
            self._close(device)

    def status(self) -> dict[str, object]:
        with self._lock:
            state = self._state
            error = self._last_error
        return {
            "enabled": self.enabled,
            "state": state,
            "talk_pin": self.talk_pin,
            "stop_pin": self.stop_pin,
            "pull_up": self.pull_up,
            "hold_seconds": self.hold_seconds,
            "last_error": error,
            **self.router.counters(),
        }

    def _edge(
        self, handler: Callable[[str], object], name: str, *_device: object
    ) -> None:
        # gpiozero dispatches on its own thread and swallows nothing, so an
        # exception here would kill that thread and silently deafen the button.
        try:
            handler(name)
        except Exception:
            LOGGER.exception("button %s handling failed", name)

    def _unavailable(self, detail: str, error: Exception) -> None:
        with self._lock:
            self._state = "unavailable"
            self._last_error = f"{detail}: {error}"[:200]
        LOGGER.warning("BMO buttons disabled: %s: %s", detail, error)

    @staticmethod
    def _close(device: object) -> None:
        try:
            close = getattr(device, "close", None)
            if close is not None:
                close()
        except Exception:
            LOGGER.exception("could not release a button pin")
