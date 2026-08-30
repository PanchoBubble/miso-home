"""Configurable, offline wake-word scoring and activation management."""

from __future__ import annotations

import logging
import os
import selectors
import struct
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from miso.audio import AudioFormat, BoundedPCMBuffer
from miso.transcription import EnergySpeechDetector


_READY = b"OWW1"
_RESET = 0xFFFFFFFF
LOGGER = logging.getLogger("miso.wake")

# Sources a wake event can carry. The model source is openWakeWord itself;
# the button source is a physical press on the enclosure, which is already an
# explicit address and so needs no acknowledgement cue before listening.
WAKE_SOURCE_MODEL = "model"
WAKE_SOURCE_BUTTON = "button"


class WakeWordError(RuntimeError):
    """Raised when the local wake-word model cannot score audio."""


class WakeModel(Protocol):
    model: Path

    def available(self) -> bool: ...

    def score(self, pcm: bytes) -> float: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class CaptureBus(Protocol):
    audio_format: AudioFormat

    def subscribe_capture(self, capacity: int | None = None) -> BoundedPCMBuffer: ...

    def unsubscribe_capture(self, subscriber: BoundedPCMBuffer) -> None: ...


@dataclass(frozen=True, slots=True)
class WakeEvent:
    phrase: str
    score: float
    detected_at: float
    source: str = "model"

    def as_dict(self) -> dict[str, object]:
        return {
            "phrase": self.phrase,
            "score": round(self.score, 4),
            "detected_at": round(self.detected_at, 3),
            "source": self.source,
        }


class OpenWakeWordModel:
    """Run openWakeWord in a pinned, isolated Python environment."""

    def __init__(
        self,
        executable: Path,
        model: Path,
        *,
        vad_threshold: float,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not 0 <= vad_threshold <= 1:
            raise ValueError("wake VAD threshold must be between 0 and 1")
        if timeout_seconds <= 0:
            raise ValueError("wake model timeout must be positive")
        self.executable = executable
        self.model = model
        self.vad_threshold = vad_threshold
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def available(self) -> bool:
        return (
            self.executable.is_file()
            and os.access(self.executable, os.X_OK)
            and self.model.is_file()
        )

    def score(self, pcm: bytes) -> float:
        if not pcm or len(pcm) % 2:
            raise ValueError("wake PCM must contain complete S16_LE samples")
        with self._lock:
            process = self._ensure_process()
            try:
                assert process.stdin is not None
                process.stdin.write(struct.pack(">I", len(pcm)))
                process.stdin.write(pcm)
                process.stdin.flush()
                response = self._read_exact(process, 5)
                status, score = struct.unpack(">Bf", response)
                if status:
                    size = struct.unpack(">I", self._read_exact(process, 4))[0]
                    detail = self._read_exact(process, min(size, 4096)).decode(
                        "utf-8", "replace"
                    )
                    if size > 4096:
                        self._read_exact(process, size - 4096)
                    raise WakeWordError(f"openWakeWord scoring failed: {detail}")
                return min(1.0, max(0.0, float(score)))
            except (BrokenPipeError, OSError, struct.error, WakeWordError):
                self._close_locked()
                raise

    def reset(self) -> None:
        with self._lock:
            if self._process is None:
                return
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(struct.pack(">I", _RESET))
                self._process.stdin.flush()
                status, _score = struct.unpack(
                    ">Bf", self._read_exact(self._process, 5)
                )
                if status:
                    raise WakeWordError("openWakeWord reset failed")
            except (BrokenPipeError, OSError, struct.error, WakeWordError):
                self._close_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        if not self.available():
            raise WakeWordError("openWakeWord executable or model is unavailable")
        process = subprocess.Popen(
            [
                str(self.executable),
                "-m",
                "miso.openwakeword_worker",
                "--model",
                str(self.model),
                "--vad-threshold",
                str(self.vad_threshold),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            ready = self._read_exact(process, len(_READY))
        except WakeWordError as error:
            detail = b""
            if process.stderr is not None and process.poll() is not None:
                detail = process.stderr.read(4096)
            process.kill()
            process.wait(timeout=1)
            suffix = detail.decode("utf-8", "replace").strip()
            raise WakeWordError(
                "openWakeWord worker failed to start"
                + (f": {suffix[:200]}" if suffix else "")
            ) from error
        if ready != _READY:
            process.kill()
            process.wait(timeout=1)
            raise WakeWordError("openWakeWord worker returned an invalid handshake")
        self._process = process
        return process

    def _read_exact(self, process: subprocess.Popen[bytes], size: int) -> bytes:
        if process.stdout is None:
            raise WakeWordError("openWakeWord worker output is unavailable")
        result = bytearray()
        deadline = time.monotonic() + self.timeout_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while len(result) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WakeWordError("openWakeWord worker timed out")
                chunk = os.read(process.stdout.fileno(), size - len(result))
                if not chunk:
                    raise WakeWordError("openWakeWord worker stopped unexpectedly")
                result.extend(chunk)
        finally:
            selector.close()
        return bytes(result)

    def _close_locked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class WakeDetector:
    """Combine local speech gating with debounced wake-model predictions."""

    def __init__(
        self,
        model: WakeModel,
        audio_format: AudioFormat,
        *,
        phrase: str,
        threshold: float,
        energy_threshold_dbfs: float,
        activation_frames: int,
        cooldown_seconds: float,
    ) -> None:
        if audio_format.sample_rate != 16_000 or audio_format.channels != 1:
            raise ValueError("openWakeWord requires mono 16000 Hz audio")
        if audio_format.sample_width != 2:
            raise ValueError("openWakeWord requires S16_LE audio")
        if not phrase or len(phrase) > 80:
            raise ValueError("wake phrase must contain 1 to 80 characters")
        if not 0 < threshold <= 1:
            raise ValueError("wake threshold must be between 0 and 1")
        if not -120 <= energy_threshold_dbfs <= 0:
            raise ValueError("wake energy threshold must be between -120 and 0")
        if not 1 <= activation_frames <= 20:
            raise ValueError("wake activation frames must be between 1 and 20")
        if not 0 <= cooldown_seconds <= 60:
            raise ValueError("wake cooldown must be between 0 and 60 seconds")
        self.model = model
        self.phrase = phrase
        self.threshold = threshold
        self.activation_frames = activation_frames
        self.cooldown_seconds = cooldown_seconds
        self.energy_detector = EnergySpeechDetector(energy_threshold_dbfs)
        self.frame_bytes = 1_280 * 2
        self._pending = bytearray()
        self._streak = 0
        self._cooldown_until = 0.0
        self.highest_score = 0.0

    def feed(self, pcm: bytes, *, now: float | None = None) -> tuple[WakeEvent, ...]:
        if len(pcm) % 2:
            raise ValueError("wake PCM must contain complete S16_LE samples")
        self._pending.extend(pcm)
        events: list[WakeEvent] = []
        while len(self._pending) >= self.frame_bytes:
            frame = bytes(self._pending[: self.frame_bytes])
            del self._pending[: self.frame_bytes]
            score = self.model.score(frame)
            self.highest_score = max(self.highest_score, score)
            speech = self.energy_detector.is_speech(frame)
            instant = time.time() if now is None else now
            if not speech or score < self.threshold:
                self._streak = 0
                continue
            self._streak += 1
            if self._streak < self.activation_frames:
                continue
            self._streak = 0
            if instant < self._cooldown_until:
                continue
            self._cooldown_until = instant + self.cooldown_seconds
            events.append(WakeEvent(self.phrase, score, instant))
        return tuple(events)

    def reset(self) -> None:
        self._pending.clear()
        self._streak = 0
        self._cooldown_until = 0
        self.highest_score = 0
        self.model.reset()


class WakeWordManager:
    """Consume an independent capture tap and retain bounded wake events."""

    def __init__(
        self,
        *,
        enabled: bool,
        audio: CaptureBus,
        model: WakeModel,
        phrase: str,
        threshold: float,
        energy_threshold_dbfs: float,
        activation_frames: int,
        cooldown_seconds: float,
        result_capacity: int,
        on_activation: Callable[[WakeEvent], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self.audio = audio
        self.model = model
        self.detector = WakeDetector(
            model,
            audio.audio_format if enabled else AudioFormat(),
            phrase=phrase,
            threshold=threshold,
            energy_threshold_dbfs=energy_threshold_dbfs,
            activation_frames=activation_frames,
            cooldown_seconds=cooldown_seconds,
        )
        self._events: deque[WakeEvent] = deque(maxlen=result_capacity)
        self._condition = threading.Condition()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture: BoundedPCMBuffer | None = None
        self._state = "disabled" if not enabled else "starting"
        self._last_error: str | None = None
        self._activations = 0
        self._failures = 0
        self._on_activation = on_activation

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._capture = self.audio.subscribe_capture()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="miso-wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._capture is not None:
            self._capture.wake()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._capture is not None:
            self.audio.unsubscribe_capture(self._capture)
            self._capture = None
        self.model.close()
        self._set_state("stopped" if self.enabled else "disabled")

    def get_event(self, timeout: float | None = None) -> WakeEvent | None:
        with self._condition:
            if not self._events:
                self._condition.wait(timeout)
            return self._events.popleft() if self._events else None

    def activate(self, event: WakeEvent) -> None:
        """Publish a wake confirmed by this detector or a trusted local fallback."""
        if not self.enabled:
            return
        if self._on_activation is not None:
            try:
                self._on_activation(event)
            except Exception:
                LOGGER.exception("wake activation callback failed")
        with self._condition:
            self._events.append(event)
            self._condition.notify_all()
        with self._state_lock:
            self._activations += 1

    def status(self) -> dict[str, object]:
        with self._state_lock:
            state = self._state
            error = self._last_error
            activations = self._activations
            failures = self._failures
        with self._condition:
            queued = len(self._events)
            latest = self._events[-1].as_dict() if self._events else None
        return {
            "enabled": self.enabled,
            "available": self.model.available() if self.enabled else False,
            "state": state,
            "phrase": self.detector.phrase,
            "model": self.model.model.name,
            "threshold": self.detector.threshold,
            "energy_threshold_dbfs": self.detector.energy_detector.threshold_dbfs,
            "activation_frames": self.detector.activation_frames,
            "cooldown_seconds": self.detector.cooldown_seconds,
            "activations": activations,
            "failures": failures,
            "highest_score": round(self.detector.highest_score, 4),
            "queued_events": queued,
            "last_error": error,
            "latest": latest,
        }

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._last_error = None if error is None else error[:200]

    def _run(self) -> None:
        assert self._capture is not None
        while not self._stop.is_set():
            if not self.model.available():
                self._set_state(
                    "unavailable", "openWakeWord executable or model missing"
                )
                self._stop.wait(1)
                continue
            self._set_state("listening")
            chunk = self._capture.get(timeout=0.25)
            if chunk is None:
                continue
            try:
                events = self.detector.feed(chunk)
            except (OSError, ValueError, WakeWordError) as error:
                with self._state_lock:
                    self._failures += 1
                self._set_state("recovering", str(error))
                self.model.close()
                self._stop.wait(1)
                continue
            for event in events:
                self.activate(event)
