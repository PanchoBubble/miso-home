"""Offline utterance segmentation and whisper.cpp transcription."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from miso.audio import AudioFormat


_LANGUAGE_PATTERN = re.compile(
    r"auto-detected language:\s*([a-z][a-z0-9_-]*)\s*\(p\s*=\s*([0-9.]+)\)",
    re.IGNORECASE,
)


class TranscriptionError(RuntimeError):
    """Raised when local transcription cannot produce a valid result."""


@dataclass(frozen=True, slots=True)
class Utterance:
    pcm: bytes
    sample_rate: int
    channels: int
    duration_milliseconds: int
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class SpeechActivity:
    """A VAD event used to coordinate conversational output with STT."""

    kind: str
    occurred_at: float

    def __post_init__(self) -> None:
        if self.kind not in {"started", "ended", "discarded"}:
            raise ValueError(
                "speech activity kind must be started, ended, or discarded"
            )


@dataclass(frozen=True, slots=True)
class TranscriptionToken:
    text: str
    confidence: float
    start_milliseconds: int | None
    end_milliseconds: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "start_milliseconds": self.start_milliseconds,
            "end_milliseconds": self.end_milliseconds,
        }


@dataclass(frozen=True, slots=True)
class TranscriptionSegment:
    text: str
    start_milliseconds: int
    end_milliseconds: int
    confidence: float | None
    tokens: tuple[TranscriptionToken, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "start_milliseconds": self.start_milliseconds,
            "end_milliseconds": self.end_milliseconds,
            "confidence": (
                None if self.confidence is None else round(self.confidence, 4)
            ),
            "tokens": [token.as_dict() for token in self.tokens],
        }


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    language: str
    model_language: str
    language_confidence: float | None
    confidence: float | None
    segments: tuple[TranscriptionSegment, ...]
    audio_milliseconds: int
    inference_milliseconds: int
    real_time_factor: float
    model: str
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "language": self.language,
            "model_language": self.model_language,
            "language_confidence": (
                None
                if self.language_confidence is None
                else round(self.language_confidence, 4)
            ),
            "confidence": (
                None if self.confidence is None else round(self.confidence, 4)
            ),
            "segments": [segment.as_dict() for segment in self.segments],
            "audio_milliseconds": self.audio_milliseconds,
            "inference_milliseconds": self.inference_milliseconds,
            "real_time_factor": round(self.real_time_factor, 4),
            "model": self.model,
            "truncated": self.truncated,
        }


class UtteranceAssembler:
    """Turn externally classified PCM chunks into bounded utterances."""

    def __init__(
        self,
        audio_format: AudioFormat,
        *,
        minimum_speech_milliseconds: int,
        end_silence_milliseconds: int,
        maximum_utterance_milliseconds: int,
        pre_roll_milliseconds: int,
    ) -> None:
        self.audio_format = audio_format
        self.minimum_speech_milliseconds = minimum_speech_milliseconds
        self.end_silence_milliseconds = end_silence_milliseconds
        self.maximum_utterance_milliseconds = maximum_utterance_milliseconds
        self.pre_roll_chunks = max(
            0, math.ceil(pre_roll_milliseconds / audio_format.chunk_milliseconds)
        )
        self._pre_roll: deque[bytes] = deque(maxlen=self.pre_roll_chunks or 1)
        self._chunks: list[bytes] = []
        self._active = False
        self._speech_milliseconds = 0
        self._silence_milliseconds = 0
        self._duration_milliseconds = 0

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, pcm: bytes, *, speech: bool) -> Utterance | None:
        if not pcm:
            return None
        frame_bytes = self.audio_format.channels * self.audio_format.sample_width
        if len(pcm) % frame_bytes:
            raise ValueError("PCM chunk does not contain complete frames")
        duration = round(
            len(pcm) / frame_bytes / self.audio_format.sample_rate * 1000
        )
        if not self._active:
            if not speech:
                if self.pre_roll_chunks:
                    self._pre_roll.append(bytes(pcm))
                return None
            self._active = True
            self._chunks = list(self._pre_roll)
            self._chunks.append(bytes(pcm))
            self._pre_roll.clear()
            self._duration_milliseconds = sum(
                round(
                    len(chunk)
                    / frame_bytes
                    / self.audio_format.sample_rate
                    * 1000
                )
                for chunk in self._chunks
            )
            self._speech_milliseconds = duration
            self._silence_milliseconds = 0
            return self._finish_if_needed()

        self._chunks.append(bytes(pcm))
        self._duration_milliseconds += duration
        if speech:
            self._speech_milliseconds += duration
            self._silence_milliseconds = 0
        else:
            self._silence_milliseconds += duration
        return self._finish_if_needed()

    def flush(self) -> Utterance | None:
        if not self._active:
            return None
        return self._finish(truncated=False)

    def _finish_if_needed(self) -> Utterance | None:
        if self._duration_milliseconds >= self.maximum_utterance_milliseconds:
            return self._finish(truncated=True)
        if self._silence_milliseconds >= self.end_silence_milliseconds:
            return self._finish(truncated=False)
        return None

    def _finish(self, *, truncated: bool) -> Utterance | None:
        pcm = b"".join(self._chunks)
        duration = self._duration_milliseconds
        enough_speech = self._speech_milliseconds >= self.minimum_speech_milliseconds
        self._chunks = []
        self._active = False
        self._speech_milliseconds = 0
        self._silence_milliseconds = 0
        self._duration_milliseconds = 0
        self._pre_roll.clear()
        if not enough_speech:
            return None
        return Utterance(
            pcm=pcm,
            sample_rate=self.audio_format.sample_rate,
            channels=self.audio_format.channels,
            duration_milliseconds=duration,
            truncated=truncated,
        )


class EnergySpeechDetector:
    """Small replaceable VAD gate for S16_LE capture chunks."""

    def __init__(self, threshold_dbfs: float) -> None:
        self.threshold_dbfs = threshold_dbfs

    def is_speech(self, pcm: bytes) -> bool:
        usable = len(pcm) - len(pcm) % 2
        if not usable:
            return False
        samples = array("h")
        samples.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples:
            return False
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        dbfs = -120.0 if rms <= 0 else 20.0 * math.log10(rms / 32767.0)
        return dbfs >= self.threshold_dbfs


class CaptureSource(Protocol):
    audio_format: AudioFormat

    def read_capture(self, timeout: float | None = None) -> bytes | None: ...


class Transcriber(Protocol):
    model: Path

    def available(self) -> bool: ...

    def transcribe(self, utterance: Utterance) -> TranscriptionResult: ...


class WhisperCppTranscriber:
    """Invoke a pinned whisper.cpp CLI and normalize its full JSON output."""

    def __init__(
        self,
        executable: Path,
        model: Path,
        *,
        threads: int,
        timeout_seconds: float,
        work_directory: Path | None = None,
        prompt: str = "",
    ) -> None:
        self.executable = executable
        self.model = model
        self.threads = threads
        self.timeout_seconds = timeout_seconds
        self.work_directory = work_directory
        self.prompt = prompt

    def available(self) -> bool:
        return (
            self.executable.is_file()
            and os.access(self.executable, os.X_OK)
            and self.model.is_file()
        )

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise TranscriptionError("whisper.cpp executable is unavailable")
        if not self.model.is_file():
            raise TranscriptionError("whisper.cpp model is unavailable")
        temporary_parent = (
            self.work_directory
            if self.work_directory is not None and self.work_directory.is_dir()
            else None
        )
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(
                prefix="miso-stt-", dir=temporary_parent
            ) as directory:
                root = Path(directory)
                audio_path = root / "utterance.wav"
                output_prefix = root / "result"
                with wave.open(str(audio_path), "wb") as output:
                    output.setnchannels(utterance.channels)
                    output.setsampwidth(2)
                    output.setframerate(utterance.sample_rate)
                    output.writeframes(utterance.pcm)
                command = [
                    str(self.executable),
                    "--model",
                    str(self.model),
                    "--file",
                    str(audio_path),
                    "--language",
                    "auto",
                    "--threads",
                    str(self.threads),
                    "--output-json-full",
                    "--output-file",
                    str(output_prefix),
                    "--no-gpu",
                ]
                if self.prompt:
                    command.extend(("--prompt", self.prompt))
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=self.timeout_seconds,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                if completed.returncode:
                    detail = completed.stderr.strip().splitlines()
                    suffix = f": {detail[-1][:200]}" if detail else ""
                    raise TranscriptionError(f"whisper.cpp failed{suffix}")
                result_path = output_prefix.with_suffix(".json")
                try:
                    payload = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise TranscriptionError(
                        "whisper.cpp returned invalid JSON"
                    ) from error
        except subprocess.TimeoutExpired as error:
            raise TranscriptionError("whisper.cpp transcription timed out") from error

        elapsed = max(0, round((time.monotonic() - started) * 1000))
        return self._result(payload, completed.stderr, utterance, elapsed)

    def _result(
        self,
        payload: object,
        diagnostics: str,
        utterance: Utterance,
        inference_milliseconds: int,
    ) -> TranscriptionResult:
        if not isinstance(payload, dict):
            raise TranscriptionError("whisper.cpp JSON root is invalid")
        raw_result = payload.get("result")
        model_language = (
            raw_result.get("language") if isinstance(raw_result, dict) else None
        )
        if not isinstance(model_language, str) or not model_language:
            raise TranscriptionError("whisper.cpp omitted detected language")
        language_match = _LANGUAGE_PATTERN.search(diagnostics)
        language_confidence = None
        if (
            language_match
            and language_match.group(1).casefold() == model_language.casefold()
        ):
            language_confidence = _probability(language_match.group(2))

        raw_segments = payload.get("transcription")
        if not isinstance(raw_segments, list):
            raise TranscriptionError("whisper.cpp omitted transcription segments")
        segments: list[TranscriptionSegment] = []
        all_confidences: list[float] = []
        for raw_segment in raw_segments:
            segment = _parse_segment(raw_segment)
            if segment is None:
                continue
            segments.append(segment)
            all_confidences.extend(
                token.confidence
                for token in segment.tokens
                if not _special_token(token.text)
            )
        text = " ".join(
            segment.text.strip() for segment in segments if segment.text.strip()
        )
        language = "mixed" if _english_spanish_code_switch(text) else model_language
        confidence = (
            sum(all_confidences) / len(all_confidences)
            if all_confidences
            else None
        )
        audio_milliseconds = max(1, utterance.duration_milliseconds)
        return TranscriptionResult(
            text=text,
            language=language,
            model_language=model_language,
            language_confidence=language_confidence,
            confidence=confidence,
            segments=tuple(segments),
            audio_milliseconds=utterance.duration_milliseconds,
            inference_milliseconds=inference_milliseconds,
            real_time_factor=inference_milliseconds / audio_milliseconds,
            model=self.model.name,
            truncated=utterance.truncated,
        )


class TranscriptionManager:
    """Consume captured utterances and retain a bounded result stream."""

    def __init__(
        self,
        *,
        enabled: bool,
        audio: CaptureSource,
        transcriber: Transcriber,
        detector: EnergySpeechDetector,
        assembler: UtteranceAssembler,
        result_capacity: int,
    ) -> None:
        self.enabled = enabled
        self.audio = audio
        self.transcriber = transcriber
        self.detector = detector
        self.assembler = assembler
        self._results: deque[TranscriptionResult] = deque(maxlen=result_capacity)
        self._activity: deque[SpeechActivity] = deque(maxlen=result_capacity * 2)
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._state = "disabled" if not enabled else "starting"
        self._last_error: str | None = None
        self._processed = 0
        self._failures = 0

    def start(self) -> None:
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="miso-transcription", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._set_state("stopped" if self.enabled else "disabled")

    def get_result(self, timeout: float | None = None) -> TranscriptionResult | None:
        with self._condition:
            if not self._results:
                self._condition.wait(timeout)
            return self._results.popleft() if self._results else None

    def get_activity(self, timeout: float | None = None) -> SpeechActivity | None:
        with self._condition:
            if not self._activity:
                self._condition.wait(timeout)
            return self._activity.popleft() if self._activity else None

    def status(self) -> dict[str, object]:
        with self._state_lock:
            state = self._state
            error = self._last_error
            processed = self._processed
            failures = self._failures
        with self._condition:
            queued = len(self._results)
            queued_activity = len(self._activity)
            latest = self._results[-1] if self._results else None
        latest_summary = None
        if latest is not None:
            latest_summary = {
                "language": latest.language,
                "model_language": latest.model_language,
                "language_confidence": latest.language_confidence,
                "confidence": latest.confidence,
                "audio_milliseconds": latest.audio_milliseconds,
                "inference_milliseconds": latest.inference_milliseconds,
                "real_time_factor": round(latest.real_time_factor, 4),
                "truncated": latest.truncated,
            }
        return {
            "enabled": self.enabled,
            "available": self.transcriber.available() if self.enabled else False,
            "state": state,
            "model": self.transcriber.model.name,
            "processed": processed,
            "failures": failures,
            "queued_results": queued,
            "queued_activity": queued_activity,
            "last_error": error,
            "latest": latest_summary,
        }

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._last_error = None if error is None else error[:200]

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.transcriber.available():
                self._set_state("unavailable", "whisper.cpp executable or model missing")
                self._stop.wait(1)
                continue
            self._set_state("listening")
            chunk = self.audio.read_capture(timeout=0.25)
            if chunk is None:
                continue
            was_active = self.assembler.active
            utterance = self.assembler.feed(
                chunk, speech=self.detector.is_speech(chunk)
            )
            is_active = self.assembler.active
            if is_active != was_active:
                kind = (
                    "started"
                    if is_active
                    else ("discarded" if utterance is None else "ended")
                )
                with self._condition:
                    self._activity.append(SpeechActivity(kind, time.time()))
                    self._condition.notify_all()
            if utterance is None:
                continue
            self._set_state("transcribing")
            try:
                result = self.transcriber.transcribe(utterance)
            except TranscriptionError as error:
                with self._state_lock:
                    self._failures += 1
                self._set_state("listening", str(error))
                continue
            with self._condition:
                self._results.append(result)
                self._condition.notify_all()
            with self._state_lock:
                self._processed += 1
            self._set_state("listening")


def _parse_segment(raw: object) -> TranscriptionSegment | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    offsets = raw.get("offsets")
    if not isinstance(text, str) or not isinstance(offsets, dict):
        return None
    start = offsets.get("from")
    end = offsets.get("to")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    tokens: list[TranscriptionToken] = []
    raw_tokens = raw.get("tokens", [])
    if isinstance(raw_tokens, list):
        for raw_token in raw_tokens:
            token = _parse_token(raw_token)
            if token is not None and not _special_token(token.text):
                tokens.append(token)
    confidences = [
        token.confidence for token in tokens if not _special_token(token.text)
    ]
    confidence = sum(confidences) / len(confidences) if confidences else None
    return TranscriptionSegment(text, start, end, confidence, tuple(tokens))


def _parse_token(raw: object) -> TranscriptionToken | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return None
    confidence = _probability(raw.get("p"))
    if confidence is None:
        return None
    start: int | None = None
    end: int | None = None
    offsets = raw.get("offsets")
    if isinstance(offsets, dict):
        raw_start = offsets.get("from")
        raw_end = offsets.get("to")
        if isinstance(raw_start, int) and isinstance(raw_end, int):
            start, end = raw_start, raw_end
    return TranscriptionToken(raw["text"], confidence, start, end)


def _probability(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(1.0, max(0.0, number))


def _special_token(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("<|") and stripped.endswith("|>")


def _english_spanish_code_switch(text: str) -> bool:
    words = set(normalized_words(text))
    english = words.intersection(
        {
            "add",
            "and",
            "bread",
            "coffee",
            "for",
            "kitchen",
            "light",
            "list",
            "minutes",
            "set",
            "shopping",
            "the",
            "timer",
            "to",
            "turn",
        }
    )
    spanish = words.intersection(
        {
            "anade",
            "apaga",
            "cocina",
            "compras",
            "de",
            "del",
            "diez",
            "enciende",
            "la",
            "leche",
            "lista",
            "luz",
            "minutos",
            "pon",
            "salon",
            "temporizador",
            "un",
            "y",
        }
    )
    return len(english) >= 2 and len(spanish) >= 2


def normalized_words(text: str) -> tuple[str, ...]:
    """Normalize English/Spanish text for deterministic benchmark scoring."""

    normalized = unicodedata.normalize("NFKD", text.casefold())
    without_marks = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    cleaned = "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in without_marks
    )
    return tuple(cleaned.split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Return Levenshtein word error rate, preserving errors above 100%."""

    expected = normalized_words(reference)
    actual = normalized_words(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    previous = list(range(len(actual) + 1))
    for row, expected_word in enumerate(expected, start=1):
        current = [row]
        for column, actual_word in enumerate(actual, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (expected_word != actual_word),
                )
            )
        previous = current
    return previous[-1] / len(expected)
