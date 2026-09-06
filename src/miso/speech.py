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
        self._ready: dict[str, threading.Event] = {}
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
                process = existing
                ready = self._ready[language]
                spawned = False
            else:
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
                ready = threading.Event()
                self._processes[language] = process
                self._ready[language] = ready
                spawned = True
        deadline = time.monotonic() + self.timeout_seconds
        if spawned:
            value = self._read_size(process, cancel_event, deadline)
            if value != 0xFFFFFFFF:
                self._discard_worker(language, process)
                raise SpeechError("Piper worker did not become ready")
            ready.set()
            return process
        # Another thread (usually a rewarm after a barge-in) is still reading
        # the READY frame from this process; reading alongside it would swallow
        # that frame as an audio size.
        while not ready.is_set():
            if cancel_event.is_set():
                raise InterruptedError("speech synthesis was cancelled")
            if time.monotonic() >= deadline:
                raise SpeechError("Piper worker did not become ready")
            if process.poll() is not None:
                raise SpeechError("Piper worker stopped unexpectedly")
            ready.wait(0.01)
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


@dataclass(slots=True)
class _SpeechRequest:
    request_id: str
    text: str
    language: str
    volume: float
    cancel_event: threading.Event
    enqueued_at: float


@dataclass(slots=True)
class _Synthesized:
    """A request whose PCM is fully queued on the sink and awaiting drain."""

    request: _SpeechRequest
    metrics: SynthesisMetrics
    first_audio_milliseconds: int | None
    synthesis_milliseconds: int


class SpeechManager:
    """Coordinate a queue of speech requests and bounded result history.

    Requests queued with ``enqueue`` form one utterance: synthesis of request
    N+1 begins as soon as request N's PCM is handed to the audio sink, not
    after it has played, so sentence boundaries carry no synthesis gap. The
    sink only drains once the queue is empty. ``speak`` cancels whatever is
    active or queued first, which is what cues and the HTTP API want.
    """

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
        self._queue: deque[_SpeechRequest] = deque()
        self._active: _SpeechRequest | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state = "disabled" if not enabled else "idle"
        self._last_error: str | None = None
        self._results: dict[str, SpeechResult] = {}
        self._result_order: deque[str] = deque()

    def start(self) -> None:
        start = getattr(self.backend, "start", None)
        if self.enabled and start is not None:
            start()

    def stop(self) -> None:
        self._stop_event.set()
        self.cancel()
        with self._condition:
            self._condition.notify_all()
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
        """Replace anything active or queued with this text."""
        request = self._validate(text, language, volume)
        self.cancel()
        return self._submit(request)

    def enqueue(
        self, text: str, language: str, *, volume: float | None = None
    ) -> str:
        """Append this text to the current utterance without interrupting it."""
        request = self._validate(text, language, volume)
        return self._submit(request)

    def _validate(
        self, text: str, language: str, volume: float | None
    ) -> _SpeechRequest:
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
        return _SpeechRequest(
            request_id=str(uuid.uuid4()),
            text=normalized,
            language=language,
            volume=selected_volume,
            cancel_event=threading.Event(),
            enqueued_at=time.monotonic(),
        )

    def _submit(self, request: _SpeechRequest) -> str:
        with self._condition:
            if self._stop_event.is_set():
                raise SpeechError("speech synthesis is stopped")
            self._queue.append(request)
            if self._active is None:
                self._state = "synthesizing"
            self._last_error = None
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._serve, name="miso-speech", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()
        return request.request_id

    def cancel(self, request_id: str | None = None) -> bool:
        """Cancel the whole utterance: the active request and everything queued.

        A queued sentence only makes sense after the one before it, so
        cancelling any single request drops the rest of the utterance too.
        """
        with self._condition:
            active = self._active
            queued = tuple(self._queue)
            known = {item.request_id for item in queued}
            if active is not None:
                known.add(active.request_id)
            if not known or (request_id is not None and request_id not in known):
                return False
            self._queue.clear()
            for item in queued:
                self._record_locked(
                    self._result(item, "cancelled", None, None, 0, 0, None)
                )
            if active is not None:
                active.cancel_event.set()
                self._state = "cancelling"
            else:
                self._state = "idle"
            self._condition.notify_all()
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
                "active_request_id": (
                    None if self._active is None else self._active.request_id
                ),
                "queued": len(self._queue),
                "default_volume": self.default_volume,
                "voices": [
                    voice.public_dict() for voice in self.backend.voices.values()
                ],
                "last_error": self._last_error,
                "latest": None if latest is None else latest.as_dict(),
            }

    def _serve(self) -> None:
        pending: list[_Synthesized] = []
        drain_deadline = 0.0
        while not self._stop_event.is_set():
            with self._condition:
                request = self._queue.popleft() if self._queue else None
                if request is not None:
                    self._active = request
                    self._state = "synthesizing"
                elif not pending:
                    self._active = None
                    self._state = "idle"
                    self._condition.wait(0.25)
                    continue
            if any(item.request.cancel_event.is_set() for item in pending):
                # cancel() already cleared the sink; only the bookkeeping is left.
                self._resolve(pending, "cancelled")
            if request is None:
                # Nothing left to synthesize: let the sink run dry, but keep
                # checking the queue so a late sentence starts synthesizing
                # while the previous one is still sounding.
                if pending:
                    self._drain(pending, drain_deadline)
                continue
            synthesized = self._synthesize(request, pending)
            if synthesized is not None:
                pending.append(synthesized)
                audio_seconds = sum(
                    item.metrics.audio_milliseconds for item in pending
                ) / 1000
                drain_deadline = time.monotonic() + max(2.0, audio_seconds + 2)

    def _synthesize(
        self, request: _SpeechRequest, pending: list[_Synthesized]
    ) -> _Synthesized | None:
        cancel_event = request.cancel_event
        started = time.monotonic()
        queue_wait = round((started - request.enqueued_at) * 1000)

        def on_audio(chunk: bytes) -> None:
            if cancel_event.is_set():
                return
            with self._lock:
                if self._active is request:
                    self._state = "playing"
            self.audio.play_stream(chunk, timeout=1.0, cancel_event=cancel_event)

        try:
            metrics = self.backend.synthesize(
                request.text, request.language, request.volume, cancel_event, on_audio
            )
        except (SpeechError, RuntimeError, OSError) as error:
            elapsed = round((time.monotonic() - started) * 1000)
            if cancel_event.is_set():
                self.audio.cancel_playback()
                self._resolve(pending, "cancelled")
                self._record(
                    self._result(request, "cancelled", None, elapsed, 0, 0, None)
                )
                return None
            # Earlier sentences are already on the sink and still make sense on
            # their own, so they keep playing; only this request fails.
            self._record(
                self._result(
                    request, "error", None, elapsed, 0, 0, str(error)[:200]
                )
            )
            return None
        elapsed = round((time.monotonic() - started) * 1000)
        if metrics.cancelled or cancel_event.is_set():
            self.audio.cancel_playback()
            self._resolve(pending, "cancelled")
            self._record(
                self._result(
                    request,
                    "cancelled",
                    metrics.first_audio_milliseconds,
                    elapsed,
                    metrics.audio_milliseconds,
                    metrics.chunks,
                    None,
                    voice=metrics.voice.name,
                )
            )
            return None
        first_audio = (
            None
            if metrics.first_audio_milliseconds is None
            else metrics.first_audio_milliseconds + queue_wait
        )
        return _Synthesized(request, metrics, first_audio, elapsed)

    def _drain(self, pending: list[_Synthesized], deadline: float) -> None:
        if self.audio.wait_playback(0.05):
            self._resolve(pending, "completed")
            return
        if time.monotonic() >= deadline:
            self.audio.cancel_playback()
            self._resolve(
                pending, "error", "audio playback did not drain before timeout"
            )

    def _resolve(
        self, pending: list[_Synthesized], status: str, error: str | None = None
    ) -> None:
        items = list(pending)
        pending.clear()
        with self._condition:
            for item in items:
                self._record_locked(
                    self._result(
                        item.request,
                        status,
                        item.first_audio_milliseconds,
                        item.synthesis_milliseconds,
                        item.metrics.audio_milliseconds,
                        item.metrics.chunks,
                        error,
                        voice=item.metrics.voice.name,
                    )
                )

    def _result(
        self,
        request: _SpeechRequest,
        status: str,
        first_audio_milliseconds: int | None,
        synthesis_milliseconds: int | None,
        audio_milliseconds: int,
        chunks: int,
        error: str | None,
        *,
        voice: str | None = None,
    ) -> SpeechResult:
        now = time.monotonic()
        return SpeechResult(
            request_id=request.request_id,
            status=status,
            language=request.language,
            voice=voice
            or self.backend.voices.get(
                request.language,
                PiperVoice(request.language, "unknown", Path(), Path()),
            ).name,
            first_audio_milliseconds=first_audio_milliseconds,
            synthesis_milliseconds=(
                round((now - request.enqueued_at) * 1000)
                if synthesis_milliseconds is None
                else synthesis_milliseconds
            ),
            total_milliseconds=round((now - request.enqueued_at) * 1000),
            audio_milliseconds=audio_milliseconds,
            chunks=chunks,
            error=error,
        )

    def _record(self, result: SpeechResult) -> None:
        with self._condition:
            self._record_locked(result)

    def _record_locked(self, result: SpeechResult) -> None:
        self._results[result.request_id] = result
        self._result_order.append(result.request_id)
        while len(self._result_order) > self.result_capacity:
            expired = self._result_order.popleft()
            self._results.pop(expired, None)
        if self._active is not None and self._active.request_id == result.request_id:
            self._active = None
            if not self._queue:
                self._state = "idle"
        if result.error is not None:
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
