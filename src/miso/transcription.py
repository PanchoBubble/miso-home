"""Utterance segmentation and the transcription lanes that consume it."""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import wave
from array import array
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from miso.audio import AudioFormat


_LANGUAGE_PATTERN = re.compile(
    r"auto-detected language:\s*([a-z][a-z0-9_-]*)\s*\(p\s*=\s*([0-9.]+)\)",
    re.IGNORECASE,
)


LOGGER = logging.getLogger("miso.transcription")


class TranscriptionError(RuntimeError):
    """Raised when a transcription lane cannot produce a valid result."""


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

    def reset(self) -> None:
        """Drop everything buffered so far without producing an utterance."""
        self._chunks = []
        self._active = False
        self._speech_milliseconds = 0
        self._silence_milliseconds = 0
        self._duration_milliseconds = 0
        self._pre_roll.clear()

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
    model_name: str

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

    @property
    def model_name(self) -> str:
        return self.model.name

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


def wav_bytes(utterance: Utterance) -> bytes:
    """Wrap raw capture PCM in a WAV container the transcription lanes accept."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(utterance.channels)
        output.setsampwidth(2)
        output.setframerate(utterance.sample_rate)
        output.writeframes(utterance.pcm)
    return buffer.getvalue()


def _plain_result(
    text: str,
    *,
    language: str,
    model: str,
    utterance: Utterance,
    inference_milliseconds: int,
    confidence: float | None = None,
) -> TranscriptionResult:
    """Build a result for a lane that returns text without token detail."""
    audio_milliseconds = max(1, utterance.duration_milliseconds)
    normalized = language.strip().casefold()[:5]
    return TranscriptionResult(
        text=text.strip(),
        language=normalized,
        model_language=normalized,
        language_confidence=None,
        confidence=confidence,
        segments=(),
        audio_milliseconds=utterance.duration_milliseconds,
        inference_milliseconds=inference_milliseconds,
        real_time_factor=inference_milliseconds / audio_milliseconds,
        model=model,
        truncated=utterance.truncated,
    )


class WisprFlowTranscriber:
    """Hosted Wispr Flow lane: base64 WAV over one warm HTTPS connection.

    Flow is a dictation model rather than a raw recogniser, so it removes
    filler words and repairs names on its own. Audio leaves the house on this
    lane; the local lanes below are what runs when it is unavailable.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://platform-api.wisprflow.ai/api/v1/dash",
        languages: tuple[str, ...] = ("en", "es"),
        timeout_seconds: float = 4.0,
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self.base_url = base_url.rstrip("/")
        self.languages = tuple(code.casefold() for code in languages if code.strip())
        self.timeout_seconds = timeout_seconds
        self.model = Path("wispr-flow")
        self._warmed_at = 0.0

    @property
    def name(self) -> str:
        return "wispr-flow"

    @property
    def model_name(self) -> str:
        return "wispr-flow"

    def available(self) -> bool:
        return self._api_key is not None

    def warm_up(self) -> None:
        """Complete the TLS handshake before there is an utterance to send.

        Flow documents a warm-up endpoint precisely because the handshake is a
        visible share of a short request. The wake phrase is the natural moment
        to pay it, roughly a second before the audio is ready.
        """
        if self._api_key is None:
            return
        now = time.monotonic()
        if now - self._warmed_at < 45:
            return
        self._warmed_at = now
        request = Request(f"{self.base_url}/warmup_dash", method="GET")
        try:
            with urlopen(request, timeout=min(self.timeout_seconds, 2)) as response:
                response.read(1)
        except (HTTPError, URLError, OSError) as error:
            LOGGER.debug("wispr flow warm-up failed: %s", error)

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        if self._api_key is None:
            raise TranscriptionError("wispr flow API key is not configured")
        payload = {
            "audio": base64.b64encode(wav_bytes(utterance)).decode("ascii"),
            "language": list(self.languages),
            "context": {"app": {"type": "assistant"}},
        }
        request = Request(
            f"{self.base_url}/api",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.load(response)
        except HTTPError as error:
            raise TranscriptionError(
                f"wispr flow returned HTTP {error.code}"
            ) from error
        except (URLError, OSError) as error:
            raise TranscriptionError("wispr flow is unreachable") from error
        except json.JSONDecodeError as error:
            raise TranscriptionError("wispr flow returned invalid JSON") from error
        if not isinstance(body, dict):
            raise TranscriptionError("wispr flow JSON root is invalid")
        text = body.get("text")
        if not isinstance(text, str):
            raise TranscriptionError("wispr flow omitted the transcript")
        language = body.get("detected_language")
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        return _plain_result(
            text,
            language=language if isinstance(language, str) and language else "en",
            model="wispr-flow",
            utterance=utterance,
            inference_milliseconds=elapsed,
        )


# whisper-1's verbose_json names the language in English rather than as a code.
# Without a verdict a Spanish question comes back tagged English and Miso
# answers in the wrong language, so the name is mapped back to a code.
_LANGUAGE_NAMES = {
    "english": "en",
    "spanish": "es",
    "castilian": "es",
}


def _language_code(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip().casefold()
    if raw in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[raw]
    # Already a code, or a language Miso does not speak. Either way the
    # conversation decides what to do with it.
    return raw[:5]


class OpenAITranscriber:
    """Hosted OpenAI-compatible /audio/transcriptions lane.

    The same request shape serves OpenAI, Groq, and anything else speaking that
    API, so switching provider is a base URL and a model name rather than code.
    """

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "whisper-1",
        response_format: str = "verbose_json",
        languages: tuple[str, ...] = ("en", "es"),
        timeout_seconds: float = 6.0,
    ) -> None:
        self._api_key = api_key.strip() if api_key else None
        self.base_url = base_url.rstrip("/")
        self.model = Path(model)
        self._model = model
        self.response_format = response_format
        self.languages = tuple(code.casefold() for code in languages if code.strip())
        self.timeout_seconds = timeout_seconds

    @property
    def name(self) -> str:
        return "openai"

    @property
    def model_name(self) -> str:
        return self._model

    def available(self) -> bool:
        return self._api_key is not None

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        if self._api_key is None:
            raise TranscriptionError("cloud transcription key is not configured")
        fields = {
            "model": self._model,
            "response_format": self.response_format,
            "temperature": "0",
        }
        # No language is pinned: the household speaks two, and forcing one
        # would silently transcribe the other into nonsense.
        body, content_type = _multipart(
            fields, "file", "utterance.wav", wav_bytes(utterance)
        )
        request = Request(
            f"{self.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": content_type,
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise TranscriptionError(
                f"cloud transcription returned HTTP {error.code}"
            ) from error
        except (URLError, OSError) as error:
            raise TranscriptionError("cloud transcription is unreachable") from error
        except json.JSONDecodeError as error:
            raise TranscriptionError(
                "cloud transcription returned invalid JSON"
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise TranscriptionError("cloud transcription omitted the transcript")
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        text = payload["text"]
        language = _language_code(payload.get("language"))
        if not language:
            # The 4o transcribe models answer json only and name no language.
            # Guessing from the text keeps bilingual replies working rather
            # than defaulting every Spanish answer to English.
            language = guess_language(text, self.languages)
        return _plain_result(
            text,
            language=language,
            model=self._model,
            utterance=utterance,
            inference_milliseconds=elapsed,
        )


class WhisperServerTranscriber:
    """Local whisper.cpp server lane, which keeps the model resident.

    The CLI lane below reloads ggml-tiny from disk for every utterance, and
    that load is most of its measured latency. This lane pays it once at boot.
    """

    def __init__(
        self,
        url: str,
        *,
        model: Path,
        timeout_seconds: float = 10.0,
        prompt: str = "",
        language: str = "auto",
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.prompt = prompt
        self.language = language
        self._unreachable_until = 0.0

    @property
    def name(self) -> str:
        return "whisper-server"

    @property
    def model_name(self) -> str:
        return self.model.name

    def available(self) -> bool:
        if time.monotonic() < self._unreachable_until:
            return False
        try:
            with urlopen(f"{self.url}/", timeout=1) as response:
                response.read(1)
        except HTTPError:
            # A 404 on the root path still proves something is listening.
            return True
        except (URLError, OSError):
            self._unreachable_until = time.monotonic() + 10
            return False
        return True

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        fields = {
            "temperature": "0.0",
            "response_format": "json",
            "language": self.language,
        }
        if self.prompt:
            fields["prompt"] = self.prompt
        body, content_type = _multipart(
            fields, "file", "utterance.wav", wav_bytes(utterance)
        )
        request = Request(
            f"{self.url}/inference",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.load(response)
        except HTTPError as error:
            raise TranscriptionError(
                f"whisper server returned HTTP {error.code}"
            ) from error
        except (URLError, OSError) as error:
            self._unreachable_until = time.monotonic() + 10
            raise TranscriptionError("whisper server is unreachable") from error
        except json.JSONDecodeError as error:
            raise TranscriptionError("whisper server returned invalid JSON") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise TranscriptionError("whisper server omitted the transcript")
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        return _plain_result(
            payload["text"],
            # whisper.cpp's json format carries no language verdict. Reporting
            # a guess here would let the conversation drop a good transcript as
            # a foreign language, so the field is left for the caller to skip.
            language=str(payload.get("language") or ""),
            model=self.model.name,
            utterance=utterance,
            inference_milliseconds=elapsed,
        )


def _multipart(
    fields: dict[str, str], file_field: str, filename: str, content: bytes
) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}";'
        f' filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'.encode(
            "utf-8"
        )
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class FallbackTranscriber:
    """Try transcription lanes in order and remember which one answered.

    A lane that fails is put in a short cooldown rather than retried on the
    next utterance: when the house loses its uplink, paying the hosted lane's
    timeout on every sentence is slower than having no hosted lane at all.
    """

    def __init__(
        self,
        lanes: tuple[Transcriber, ...],
        *,
        cooldown_seconds: float = 30.0,
    ) -> None:
        if not lanes:
            raise ValueError("at least one transcription lane is required")
        self.lanes = lanes
        self.cooldown_seconds = cooldown_seconds
        self._cooldowns: dict[int, float] = {}
        self._lock = threading.Lock()
        self._lane_counts: dict[str, int] = {}
        self._last_lane: str | None = None

    @property
    def model(self) -> Path:
        return self.lanes[0].model

    @property
    def model_name(self) -> str:
        for lane in self._ready_lanes():
            return _lane_name(lane)
        return _lane_name(self.lanes[0])

    @property
    def last_lane(self) -> str | None:
        with self._lock:
            return self._last_lane

    def lane_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._lane_counts)

    def warm_up(self) -> None:
        for lane in self._ready_lanes():
            warm = getattr(lane, "warm_up", None)
            if callable(warm):
                warm()
            # Only the lane that would actually take the next utterance is
            # worth warming; warming the rest wakes services for nothing.
            return

    def available(self) -> bool:
        return any(True for _ in self._ready_lanes())

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        errors: list[str] = []
        for lane in self._ready_lanes():
            name = _lane_name(lane)
            try:
                result = lane.transcribe(utterance)
            except TranscriptionError as error:
                errors.append(f"{name}: {error}")
                LOGGER.info("transcription lane %s failed: %s", name, error)
                with self._lock:
                    self._cooldowns[id(lane)] = (
                        time.monotonic() + self.cooldown_seconds
                    )
                continue
            with self._lock:
                self._last_lane = name
                self._lane_counts[name] = self._lane_counts.get(name, 0) + 1
            return result
        raise TranscriptionError(
            "; ".join(errors) if errors else "no transcription lane is available"
        )

    def _ready_lanes(self):
        now = time.monotonic()
        for lane in self.lanes:
            with self._lock:
                until = self._cooldowns.get(id(lane), 0.0)
            if now < until:
                continue
            if not lane.available():
                continue
            yield lane


def _lane_name(lane: object) -> str:
    name = getattr(lane, "name", None)
    if isinstance(name, str) and name:
        return name
    return getattr(lane, "model_name", type(lane).__name__)


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
        gated: bool = False,
    ) -> None:
        self.enabled = enabled
        self.audio = audio
        self.transcriber = transcriber
        self.detector = detector
        self.assembler = assembler
        # An ungated worker transcribes every sound in the room forever. That
        # burns a core on the neighbours' television and, worse, hands the
        # conversation whatever it heard, so a passing remark becomes a turn.
        # When gated, only the conversation opens the microphone.
        self.gated = gated
        self._gate = threading.Event()
        if not gated:
            self._gate.set()
        self._gated_out = 0
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

    def open_gate(self) -> None:
        """Accept microphone audio because the conversation is expecting it."""
        self._gate.set()

    def warm_up(self) -> None:
        """Give the active lane a head start, off the conversation thread."""
        warm = getattr(self.transcriber, "warm_up", None)
        if not callable(warm):
            return
        threading.Thread(
            target=self._warm_up_quietly, args=(warm,),
            name="miso-transcription-warmup", daemon=True,
        ).start()

    @staticmethod
    def _warm_up_quietly(warm) -> None:
        try:
            warm()
        except Exception:
            LOGGER.debug("transcription warm-up failed", exc_info=True)

    def close_gate(self) -> None:
        """Stop assembling utterances until the conversation asks again."""
        if not self.gated:
            return
        self._gate.clear()

    @property
    def gate_open(self) -> bool:
        return self._gate.is_set()

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
            "model": self.transcriber.model_name,
            "processed": processed,
            "failures": failures,
            "queued_results": queued,
            "queued_activity": queued_activity,
            "lane": getattr(self.transcriber, "last_lane", None),
            "lane_counts": (
                self.transcriber.lane_counts()
                if callable(getattr(self.transcriber, "lane_counts", None))
                else {}
            ),
            "gated": self.gated,
            "gate_open": self._gate.is_set(),
            "gated_out_chunks": self._gated_out,
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
            self._set_state("listening" if self._gate.is_set() else "gated")
            chunk = self.audio.read_capture(timeout=0.25)
            if chunk is None:
                continue
            if not self._gate.is_set():
                # Keep draining so the shared capture buffer stays current;
                # a gate that reopens onto a second of stale room audio would
                # transcribe the sentence before the wake phrase.
                if self.assembler.active:
                    self.assembler.reset()
                self._gated_out += 1
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


# Function words, not vocabulary: they are short, extremely common, and rarely
# shared between the two languages, so a handful of them decides a sentence
# without needing a model. Only used when a lane returns no language of its own.
_SPANISH_MARKERS = frozenset(
    {
        "el", "la", "los", "las", "un", "una", "de", "del", "y", "que", "en",
        "por", "para", "con", "es", "esta", "este", "esa", "ese", "muy",
        "pon", "ponme", "enciende", "apaga", "anade", "quita", "cuanto",
        "cuando", "donde", "como", "porque", "manana", "hoy", "ahora",
        "minutos", "segundos", "horas", "luz", "cocina", "lista", "compra",
        "temporizador", "gracias", "hola", "si", "no", "mi", "me", "te", "se",
    }
)
_ENGLISH_MARKERS = frozenset(
    {
        "the", "a", "an", "of", "and", "that", "in", "for", "with", "is",
        "are", "this", "these", "those", "very", "set", "turn", "add",
        "remove", "how", "when", "where", "what", "why", "tomorrow", "today",
        "now", "minutes", "seconds", "hours", "light", "kitchen", "list",
        "shopping", "timer", "thanks", "hello", "yes", "no", "my", "me", "to",
    }
)


def guess_language(text: str, languages: tuple[str, ...] = ("en", "es")) -> str:
    """Pick English or Spanish from the words alone, for lanes that say nothing.

    Returns an empty string when the text gives no useful signal, which leaves
    the caller's existing default in charge rather than inventing a verdict.
    """
    if "es" not in languages:
        return languages[0] if languages else ""
    words = set(normalized_words(text))
    if not words:
        return ""
    spanish = len(words & _SPANISH_MARKERS)
    english = len(words & _ENGLISH_MARKERS)
    if spanish == english:
        return ""
    return "es" if spanish > english else "en"


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
