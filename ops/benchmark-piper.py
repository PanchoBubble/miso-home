#!/usr/bin/env python3
"""Benchmark bilingual Piper voices for streaming latency and cancellation."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

from miso.speech import PiperBackend, PiperVoice


CASES = (
    ("en", "Turn on the kitchen light and set a timer for five minutes."),
    ("en", "Miso, dinner is ready; please add coffee to the shopping list."),
    ("es", "Enciende la luz de la cocina y pon un temporizador de cinco minutos."),
    ("es", "Miso, la cena está lista; añade café a la lista de compras."),
)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--english-model", required=True, type=Path)
    parser.add_argument("--english-config", required=True, type=Path)
    parser.add_argument("--spanish-model", required=True, type=Path)
    parser.add_argument("--spanish-config", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--chunk-bytes", type=int, default=4096)
    parser.add_argument("--first-audio-target-ms", type=int, default=1_000)
    parser.add_argument("--cancel-target-ms", type=int, default=100)
    parser.add_argument("--wav-directory", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def percentile_95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * len(ordered) + 0.5) - 1))]


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22_050)
        output.writeframes(pcm)


def main() -> int:
    options = arguments()
    if options.repeats < 1:
        raise SystemExit("--repeats must be positive")
    voices = (
        PiperVoice("en", "en_GB-cori-medium", options.english_model, options.english_config),
        PiperVoice("es", "es_ES-davefx-medium", options.spanish_model, options.spanish_config),
    )
    backend = PiperBackend(
        options.executable,
        voices,
        chunk_bytes=options.chunk_bytes,
        timeout_seconds=60,
    )
    if not backend.available():
        raise SystemExit("Piper executable or voice files are unavailable")
    backend.start()
    attempts = []
    for case_index, (language, text) in enumerate(CASES):
        for repeat in range(options.repeats):
            chunks: list[bytes] = []
            metrics = backend.synthesize(text, language, 1, threading.Event(), chunks.append)
            rtf = metrics.synthesis_milliseconds / max(1, metrics.audio_milliseconds)
            attempts.append(
                {
                    "case": case_index + 1,
                    "language": language,
                    "repeat": repeat + 1,
                    "first_audio_milliseconds": metrics.first_audio_milliseconds,
                    "synthesis_milliseconds": metrics.synthesis_milliseconds,
                    "audio_milliseconds": metrics.audio_milliseconds,
                    "real_time_factor": round(rtf, 4),
                }
            )
            if repeat == 0 and options.wav_directory is not None:
                write_wav(
                    options.wav_directory / f"{case_index + 1}-{language}.wav",
                    b"".join(chunks),
                )

    cancel_event = threading.Event()
    first_audio = threading.Event()

    def cancel_on_first_audio(_chunk: bytes) -> None:
        first_audio.set()
        cancel_event.set()

    cancellation_started = time.monotonic()
    cancelled = backend.synthesize(
        " ".join(text for _, text in CASES) * 4,
        "en",
        1,
        cancel_event,
        cancel_on_first_audio,
    )
    backend.stop()
    cancellation_ms = round((time.monotonic() - cancellation_started) * 1000)
    stop_after_first_audio_ms = max(
        0, cancellation_ms - (cancelled.first_audio_milliseconds or cancellation_ms)
    )
    first_audio_values = [
        int(item["first_audio_milliseconds"])
        for item in attempts
        if item["first_audio_milliseconds"] is not None
    ]
    p95_first_audio = percentile_95(first_audio_values)
    median_rtf = statistics.median(float(item["real_time_factor"]) for item in attempts)
    passes = (
        p95_first_audio <= options.first_audio_target_ms
        and median_rtf < 1
        and first_audio.is_set()
        and cancelled.cancelled
        and stop_after_first_audio_ms <= options.cancel_target_ms
    )
    payload = {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "settings": {
            "repeats": options.repeats,
            "first_audio_target_milliseconds": options.first_audio_target_ms,
            "cancel_target_milliseconds": options.cancel_target_ms,
            "offline": True,
        },
        "summary": {
            "attempts": len(attempts),
            "median_first_audio_milliseconds": round(statistics.median(first_audio_values)),
            "p95_first_audio_milliseconds": p95_first_audio,
            "median_real_time_factor": round(median_rtf, 4),
            "cancel_after_first_audio_milliseconds": stop_after_first_audio_ms,
            "passes": passes,
        },
        "attempts": attempts,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if options.output is not None:
        options.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
