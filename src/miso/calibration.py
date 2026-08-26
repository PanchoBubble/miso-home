"""Bounded, one-shot wake pronunciation diagnostics over the live capture bus."""

from __future__ import annotations

import math
import sys
import threading
import time
from array import array
from typing import Protocol

from miso.audio import AudioFormat, BoundedPCMBuffer
from miso.transcription import Transcriber, TranscriptionError, Utterance


class WakeCalibrationError(RuntimeError):
    """A safe public error raised by the one-shot diagnostic."""


class WakeCalibrationBusy(WakeCalibrationError):
    pass


class WakeCalibrationComplete(WakeCalibrationError):
    pass


class CaptureBus(Protocol):
    audio_format: AudioFormat

    def subscribe_capture(self, capacity: int | None = None) -> BoundedPCMBuffer: ...

    def unsubscribe_capture(self, subscriber: BoundedPCMBuffer) -> None: ...


class WakeCalibration:
    """Capture one fixed-duration clip in memory and transcribe it locally."""

    def __init__(
        self,
        *,
        enabled: bool,
        audio: CaptureBus,
        transcriber: Transcriber,
        duration_seconds: float = 5.0,
    ) -> None:
        if not 0 < duration_seconds <= 10:
            raise ValueError(
                "wake calibration duration must be between 0 and 10 seconds"
            )
        self.enabled = enabled
        self.audio = audio
        self.transcriber = transcriber
        self.duration_seconds = duration_seconds
        self._lock = threading.Lock()
        self._completed = False

    def capture(self) -> dict[str, object]:
        if not self._lock.acquire(blocking=False):
            raise WakeCalibrationBusy("wake_calibration_busy")
        try:
            if self._completed:
                raise WakeCalibrationComplete("wake_calibration_already_completed")
            if not self.enabled or not self.transcriber.available():
                raise WakeCalibrationError("wake_calibration_unavailable")
            audio_format = self.audio.audio_format
            target_bytes = round(
                audio_format.sample_rate
                * audio_format.channels
                * audio_format.sample_width
                * self.duration_seconds
            )
            capacity = math.ceil(
                self.duration_seconds * 1000 / audio_format.chunk_milliseconds
            ) + 10
            subscriber = self.audio.subscribe_capture(capacity=capacity)
            captured = bytearray()
            deadline = time.monotonic() + self.duration_seconds + 2
            try:
                while len(captured) < target_bytes and time.monotonic() < deadline:
                    chunk = subscriber.get(timeout=0.25)
                    if chunk is not None:
                        captured.extend(chunk)
            finally:
                self.audio.unsubscribe_capture(subscriber)
            if len(captured) < target_bytes:
                raise WakeCalibrationError("wake_calibration_audio_timeout")
            del captured[target_bytes:]
            pcm = bytes(captured)
            peak_dbfs, rms_dbfs = _levels(pcm)
            try:
                result = self.transcriber.transcribe(
                    Utterance(
                        pcm=pcm,
                        sample_rate=audio_format.sample_rate,
                        channels=audio_format.channels,
                        duration_milliseconds=round(self.duration_seconds * 1000),
                    )
                )
            except TranscriptionError as error:
                raise WakeCalibrationError(
                    "wake_calibration_transcription_failed"
                ) from error
            self._completed = True
            return {
                "recognized_text": result.text,
                "language": result.language,
                "confidence": (
                    None if result.confidence is None else round(result.confidence, 4)
                ),
                "duration_milliseconds": round(self.duration_seconds * 1000),
                "peak_dbfs": peak_dbfs,
                "rms_dbfs": rms_dbfs,
                "raw_audio_retained": False,
            }
        finally:
            self._lock.release()


def _levels(pcm: bytes) -> tuple[float, float]:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return -120.0, -120.0
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    return _dbfs(peak), _dbfs(rms)


def _dbfs(amplitude: float) -> float:
    if amplitude <= 0:
        return -120.0
    return round(max(-120.0, 20 * math.log10(amplitude / 32767.0)), 2)
