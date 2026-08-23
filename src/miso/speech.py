"""Offline Piper speech synthesis with streaming playback and cancellation."""

from __future__ import annotations

import json
import os
import select
import struct
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from miso.audio import AudioFormat


class SpeechError(RuntimeError):
    """Raised when speech synthesis or playback fails."""


@dataclass(frozen=True, slots=True)
class PiperVoice:
    language: str
    name: str
    model: Path
    config: Path
    sample_rate: int = 22_050

    def available(self) -> bool:
        try:
            return self.model.is_file() and self.config.is_file()
        except OSError:
            return False

    def public_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "name": self.name,
            "sample_rate": self.sample_rate,
            "available": self.available(),
        }


@dataclass(frozen=True, slots=True)
class SynthesisMetrics:
    voice: PiperVoice
    first_audio_milliseconds: int | None
    synthesis_milliseconds: int
    audio_milliseconds: int
    chunks: int
    cancelled: bool


@dataclass(frozen=True, slots=True)
class SpeechResult:
    request_id: str
    status: str
    language: str
    voice: str
    first_audio_milliseconds: int | None
    synthesis_milliseconds: int
    total_milliseconds: int
    audio_milliseconds: int
    chunks: int
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "language": self.language,
            "voice": self.voice,
            "first_audio_milliseconds": self.first_audio_milliseconds,
            "synthesis_milliseconds": self.synthesis_milliseconds,
            "total_milliseconds": self.total_milliseconds,
            "audio_milliseconds": self.audio_milliseconds,
            "chunks": self.chunks,
            "error": self.error,
        }


class AudioSink(Protocol):
    playback_format: AudioFormat

    def play_stream(
        self,
        pcm: bytes,
        timeout: float = 1.0,
        cancel_event: threading.Event | None = None,
    ) -> None: ...

    def cancel_playback(self) -> None: ...

    def wait_playback(self, timeout: float) -> bool: ...


class SpeechBackend(Protocol):
    voices: dict[str, PiperVoice]

    def available(self) -> bool: ...

    def synthesize(
        self,
        text: str,
        language: str,
        volume: float,
        cancel_event: threading.Event,
        on_audio: Callable[[bytes], None],
    ) -> SynthesisMetrics: ...


class PiperBackend:
    """Stream PCM from pre-warmed Piper workers, one per configured voice."""

    def __init__(
        self,
        executable: Path,
        voices: tuple[PiperVoice, ...],
        *,
        chunk_bytes: int,
        timeout_seconds: float,
        worker: Path | None = None,
    ) -> None:
        self.executable = executable
        self.voices = {voice.language: voice for voice in voices}
        self.chunk_bytes = chunk_bytes - chunk_bytes % 2
        self.timeout_seconds = timeout_seconds
        self.worker = worker or Path(__file__).with_name("piper_worker.py")
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._process_lock = threading.Lock()
        self._stopped = False

    def available(self) -> bool:
        try:
            return (
                self.executable.is_file()
                and os.access(self.executable, os.X_OK)
                and self.worker.is_file()
                and bool(self.voices)
                and all(voice.available() for voice in self.voices.values())
            )
        except OSError:
            return False

    def start(self) -> None:
        if not self.available():
            return
        with self._process_lock:
            self._stopped = False
        try:
            for language in self.voices:
                self._worker(language, threading.Event())
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        with self._process_lock:
            self._stopped = True
            processes = tuple(self._processes.values())
            self._processes.clear()
        for process in processes:
            _terminate(process)

    def _worker(
        self, language: str, cancel_event: threading.Event
    ) -> subprocess.Popen[bytes]:
        with self._process_lock:
            if self._stopped:
                raise SpeechError("Piper backend is stopped")
            existing = self._processes.get(language)
            if existing is not None and existing.poll() is None:
                return existing
            voice = self.voices[language]
            process = subprocess.Popen(
                [
                    str(self.executable),
                    str(self.worker),
                    "--model",
                    str(voice.model),
                    "--config",
                    str(voice.config),
                    "--chunk-bytes",
                    str(self.chunk_bytes),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            self._processes[language] = process
        ready = self._read_size(
            process, cancel_event, time.monotonic() + self.timeout_seconds
        )
        if ready != 0xFFFFFFFF:
            self._discard_worker(language, process)
            raise SpeechError("Piper worker did not become ready")
        return process

    def _discard_worker(
        self,
        language: str,
        process: subprocess.Popen[bytes],
        *,
        rewarm: bool = False,
    ) -> None:
        with self._process_lock:
            if self._processes.get(language) is process:
                self._processes.pop(language, None)
        _terminate(process)
        if rewarm:
            threading.Thread(
                target=self._rewarm,
                args=(language,),
                name=f"miso-piper-rewarm-{language}",
                daemon=True,
            ).start()

    def _rewarm(self, language: str) -> None:
        try:
            self._worker(language, threading.Event())
        except (OSError, SpeechError, subprocess.SubprocessError):
            return

    @staticmethod
    def _read_exact(
        process: subprocess.Popen[bytes],
        size: int,
        cancel_event: threading.Event,
        deadline: float,
    ) -> bytes:
        if process.stdout is None:
            raise SpeechError("Piper audio stream is unavailable")
        result = bytearray()
        descriptor = process.stdout.fileno()
        while len(result) < size:
            if cancel_event.is_set():
                raise InterruptedError("speech synthesis was cancelled")
            if time.monotonic() >= deadline:
                raise SpeechError("Piper synthesis timed out")
            readable, _, _ = select.select((descriptor,), (), (), 0.01)
            if not readable:
                if process.poll() is not None:
                    raise SpeechError("Piper worker stopped unexpectedly")
                continue
            value = os.read(descriptor, size - len(result))
            if not value:
                raise SpeechError("Piper worker closed its audio stream")
            result.extend(value)
        return bytes(result)

    @classmethod
    def _read_size(
        cls,
        process: subprocess.Popen[bytes],
        cancel_event: threading.Event,
        deadline: float,
    ) -> int:
        encoded = cls._read_exact(process, 4, cancel_event, deadline)
        return struct.unpack(">I", encoded)[0]

    def synthesize(
        self,
        text: str,
        language: str,
        volume: float,
        cancel_event: threading.Event,
        on_audio: Callable[[bytes], None],
    ) -> SynthesisMetrics:
        voice = self.voices.get(language)
        if voice is None:
            raise SpeechError("speech language must be en or es")
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise SpeechError("Piper executable is unavailable")
        if not voice.available():
            raise SpeechError(f"Piper {language} voice is unavailable")
        started = time.monotonic()
        process = self._worker(language, cancel_event)
        first_audio_milliseconds: int | None = None
        audio_bytes = 0
        chunks = 0
        cancelled = False
        deadline = started + self.timeout_seconds
        try:
            if process.stdin is None:
                raise SpeechError("Piper text input is unavailable")
            request = json.dumps(
                {"text": text, "volume": volume}, ensure_ascii=False
            ).encode("utf-8")
            process.stdin.write(struct.pack(">I", len(request)) + request)
            process.stdin.flush()
            while True:
                size = self._read_size(process, cancel_event, deadline)
                if size == 0:
                    break
                if size == 0xFFFFFFFE:
                    error_size = self._read_size(process, cancel_event, deadline)
                    detail = self._read_exact(
                        process, error_size, cancel_event, deadline
                    ).decode("utf-8", "replace")
                    raise SpeechError(f"Piper synthesis failed: {detail[:200]}")
                if size > 16_777_216 or size % 2:
                    raise SpeechError("Piper worker returned an invalid audio frame")
                chunk = self._read_exact(process, size, cancel_event, deadline)
                if first_audio_milliseconds is None:
                    first_audio_milliseconds = round(
                        (time.monotonic() - started) * 1000
                    )
                on_audio(chunk)
                chunks += 1
                audio_bytes += len(chunk)
        except InterruptedError:
            cancelled = True
            self._discard_worker(language, process, rewarm=True)
        except SpeechError:
            self._discard_worker(language, process)
            raise
        except RuntimeError:
            self._discard_worker(language, process)
            raise
        except (OSError, subprocess.SubprocessError) as error:
            self._discard_worker(language, process)
            raise SpeechError("Piper synthesis process failed") from error
        elapsed = round((time.monotonic() - started) * 1000)
        return SynthesisMetrics(
            voice=voice,
            first_audio_milliseconds=first_audio_milliseconds,
            synthesis_milliseconds=elapsed,
            audio_milliseconds=round(audio_bytes / (voice.sample_rate * 2) * 1000),
            chunks=chunks,
            cancelled=cancelled,
        )


class SpeechManager:
    """Coordinate one cancellable speech request and bounded result history."""

    def __init__(
        self,
        *,
        enabled: bool,
        backend: SpeechBackend,
        audio: AudioSink,
        default_volume: float,
        result_capacity: int,
    ) -> None:
        self.enabled = enabled
        self.backend = backend
        self.audio = audio
        self.default_volume = default_volume
        self.result_capacity = result_capacity
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._active_id: str | None = None
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._state = "disabled" if not enabled else "idle"
        self._last_error: str | None = None
        self._results: dict[str, SpeechResult] = {}
        self._result_order: deque[str] = deque()

    def start(self) -> None:
        start = getattr(self.backend, "start", None)
        if self.enabled and start is not None:
            start()

    def stop(self) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
        stop = getattr(self.backend, "stop", None)
        if stop is not None:
            stop()
        with self._lock:
            self._state = "stopped" if self.enabled else "disabled"

    def speak(
        self, text: str, language: str, *, volume: float | None = None
    ) -> str:
        if not self.enabled:
            raise SpeechError("speech synthesis is disabled")
        normalized = text.strip()
        if not normalized:
            raise SpeechError("speech text must not be empty")
        if len(normalized) > 4_000:
            raise SpeechError("speech text must be at most 4000 characters")
        if language not in self.backend.voices:
            raise SpeechError("speech language must be en or es")
        if not self.backend.available():
            raise SpeechError("speech synthesis is unavailable")
        selected_volume = self.default_volume if volume is None else volume
        if not 0 <= selected_volume <= 2:
            raise SpeechError("speech volume must be between 0 and 2")
        self.cancel()
        previous = self._thread
        if previous is not None and previous.is_alive():
            previous.join(timeout=1)
            if previous.is_alive():
                raise SpeechError("previous speech request is still stopping")
        request_id = str(uuid.uuid4())
        cancel_event = threading.Event()
        with self._lock:
            self._active_id = request_id
            self._cancel_event = cancel_event
            self._state = "synthesizing"
            self._last_error = None
            self._thread = threading.Thread(
                target=self._run,
                args=(request_id, normalized, language, selected_volume, cancel_event),
                name="miso-speech",
                daemon=True,
            )
            self._thread.start()
        return request_id

    def cancel(self, request_id: str | None = None) -> bool:
        with self._lock:
            if self._active_id is None or (
                request_id is not None and request_id != self._active_id
            ):
                return False
            cancel_event = self._cancel_event
            self._state = "cancelling"
        if cancel_event is not None:
            cancel_event.set()
        self.audio.cancel_playback()
        return True

    def wait(self, request_id: str, timeout: float | None = None) -> SpeechResult | None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while request_id not in self._results:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return self._results[request_id]

    def status(self) -> dict[str, object]:
        with self._lock:
            latest = (
                self._results[self._result_order[-1]]
                if self._result_order
                else None
            )
            return {
                "enabled": self.enabled,
                "available": self.backend.available() if self.enabled else False,
                "state": self._state,
                "active_request_id": self._active_id,
                "default_volume": self.default_volume,
                "voices": [
                    voice.public_dict() for voice in self.backend.voices.values()
                ],
                "last_error": self._last_error,
                "latest": None if latest is None else latest.as_dict(),
            }

    def _run(
        self,
        request_id: str,
        text: str,
        language: str,
        volume: float,
        cancel_event: threading.Event,
    ) -> None:
        started = time.monotonic()

        def on_audio(chunk: bytes) -> None:
            if cancel_event.is_set():
                return
            with self._lock:
                if self._active_id == request_id:
                    self._state = "playing"
            self.audio.play_stream(chunk, timeout=0.25, cancel_event=cancel_event)

        try:
            metrics = self.backend.synthesize(
                text, language, volume, cancel_event, on_audio
            )
            cancelled = metrics.cancelled or cancel_event.is_set()
            if cancelled:
                self.audio.cancel_playback()
            elif not self.audio.wait_playback(
                max(2.0, metrics.audio_milliseconds / 1000 + 2)
            ):
                raise SpeechError("audio playback did not drain before timeout")
            result = SpeechResult(
                request_id=request_id,
                status="cancelled" if cancelled else "completed",
                language=language,
                voice=metrics.voice.name,
                first_audio_milliseconds=metrics.first_audio_milliseconds,
                synthesis_milliseconds=metrics.synthesis_milliseconds,
                total_milliseconds=round((time.monotonic() - started) * 1000),
                audio_milliseconds=metrics.audio_milliseconds,
                chunks=metrics.chunks,
            )
        except (SpeechError, RuntimeError, OSError) as error:
            self.audio.cancel_playback()
            result = SpeechResult(
                request_id=request_id,
                status="cancelled" if cancel_event.is_set() else "error",
                language=language,
                voice=self.backend.voices.get(
                    language, PiperVoice(language, "unknown", Path(), Path())
                ).name,
                first_audio_milliseconds=None,
                synthesis_milliseconds=round((time.monotonic() - started) * 1000),
                total_milliseconds=round((time.monotonic() - started) * 1000),
                audio_milliseconds=0,
                chunks=0,
                error=None if cancel_event.is_set() else str(error)[:200],
            )
        with self._condition:
            self._results[request_id] = result
            self._result_order.append(request_id)
            while len(self._result_order) > self.result_capacity:
                expired = self._result_order.popleft()
                self._results.pop(expired, None)
            if self._active_id == request_id:
                self._active_id = None
                self._cancel_event = None
                self._state = "idle"
                self._last_error = result.error
            self._condition.notify_all()


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
