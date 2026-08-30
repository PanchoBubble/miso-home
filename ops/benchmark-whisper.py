#!/usr/bin/env python3
"""Benchmark whisper.cpp models against a bilingual WAV manifest."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import wave
from pathlib import Path

from miso.conversation import strip_wake_phrase
from miso.intake import match_fast_intent
from miso.transcription import Utterance, WhisperCppTranscriber, word_error_rate


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--executable", type=Path, required=True)
    result.add_argument("--model", action="append", type=Path, required=True)
    result.add_argument("--manifest", type=Path, required=True)
    result.add_argument("--threads", type=int, default=4)
    result.add_argument("--timeout", type=float, default=120)
    result.add_argument("--prompt", default="")
    result.add_argument("--wake-phrase", default="Miso")
    result.add_argument("--repeats", type=int, default=1)
    result.add_argument("--monolingual-wer-target", type=float, default=0.20)
    result.add_argument("--mixed-wer-target", type=float, default=0.50)
    result.add_argument("--latency-target-ms", type=int, default=2_500)
    result.add_argument("--intent-accuracy-target", type=float, default=1.0)
    result.add_argument("--output", type=Path)
    return result


def read_manifest(path: Path) -> list[dict[str, str]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ValueError("manifest must be a non-empty JSON array")
    cases = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("manifest cases must be objects")
        case = {}
        for key in ("id", "audio", "language", "text"):
            item = raw.get(key)
            if not isinstance(item, str) or not item:
                raise ValueError(f"manifest case has invalid {key}")
            case[key] = item
        category = raw.get("category", "monolingual")
        if category not in {"monolingual", "mixed"}:
            raise ValueError("manifest category must be monolingual or mixed")
        case["category"] = category
        intent = raw.get("intent")
        if intent is not None:
            if not isinstance(intent, str) or not intent:
                raise ValueError("manifest intent must be a non-empty string")
            arguments = raw.get("intent_arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("manifest intent_arguments must be an object")
            case["intent"] = intent
            case["intent_arguments"] = arguments
        cases.append(case)
    return cases


def intent_outcome(
    case: dict[str, object], hypothesis: str, wake_phrase: str
) -> dict[str, object] | None:
    """Score whether a transcript still reaches its deterministic fast lane.

    Word error rate alone hides the failure that matters here: a near-miss on
    one number word costs little WER but sends the whole turn to the model.
    """
    if "intent" not in case:
        return None
    request = strip_wake_phrase(hypothesis, wake_phrase)
    matched = match_fast_intent(request, str(case["language"]))
    name = None if matched is None else matched[0]
    arguments = {} if matched is None else dict(matched[1])
    return {
        "expected": case["intent"],
        "matched": name,
        "arguments": arguments,
        "hit": name == case["intent"] and arguments == case["intent_arguments"],
    }


def load_utterance(path: Path) -> Utterance:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        pcm = source.readframes(frames)
    if sample_width != 2 or channels != 1 or sample_rate != 16_000:
        raise ValueError(f"{path} must be mono 16 kHz S16_LE WAV")
    duration = round(frames / sample_rate * 1000)
    return Utterance(pcm, sample_rate, channels, duration)


def percentile_95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(0.95 * len(ordered) + 0.5) - 1))]


def benchmark(arguments: argparse.Namespace) -> dict[str, object]:
    cases = read_manifest(arguments.manifest)
    results = []
    for model in arguments.model:
        transcriber = WhisperCppTranscriber(
            arguments.executable,
            model,
            threads=arguments.threads,
            timeout_seconds=arguments.timeout,
            prompt=arguments.prompt,
        )
        model_cases = []
        for case in cases:
            audio_path = (arguments.manifest.parent / case["audio"]).resolve()
            utterance = load_utterance(audio_path)
            attempts = []
            for _ in range(arguments.repeats):
                transcription = transcriber.transcribe(utterance)
                attempts.append(
                    {
                        "text": transcription.text,
                        "intent": intent_outcome(
                            case, transcription.text, arguments.wake_phrase
                        ),
                        "language": transcription.language,
                        "language_confidence": transcription.language_confidence,
                        "confidence": transcription.confidence,
                        "inference_milliseconds": transcription.inference_milliseconds,
                        "real_time_factor": round(transcription.real_time_factor, 4),
                        "wer": round(
                            word_error_rate(case["text"], transcription.text), 4
                        ),
                    }
                )
            model_cases.append(
                {
                    "id": case["id"],
                    "expected_language": case["language"],
                    "expected_text": case["text"],
                    "category": case["category"],
                    "audio_milliseconds": utterance.duration_milliseconds,
                    "attempts": attempts,
                }
            )
        flat = [attempt for case in model_cases for attempt in case["attempts"]]
        latencies = [attempt["inference_milliseconds"] for attempt in flat]
        monolingual_attempts = [
            attempt
            for case in model_cases
            if case["category"] == "monolingual"
            for attempt in case["attempts"]
        ]
        mixed_attempts = [
            attempt
            for case in model_cases
            if case["category"] == "mixed"
            for attempt in case["attempts"]
        ]
        monolingual_wer = statistics.fmean(
            attempt["wer"] for attempt in monolingual_attempts
        )
        mixed_wer = (
            statistics.fmean(attempt["wer"] for attempt in mixed_attempts)
            if mixed_attempts
            else None
        )
        language_accuracy = statistics.fmean(
            attempt["language"] == case["expected_language"]
            for case in model_cases
            for attempt in case["attempts"]
        )
        scored_intents = [
            attempt["intent"] for attempt in flat if attempt["intent"] is not None
        ]
        intent_accuracy = (
            statistics.fmean(outcome["hit"] for outcome in scored_intents)
            if scored_intents
            else None
        )
        p95_latency = percentile_95(latencies)
        results.append(
            {
                "model": model.name,
                "model_bytes": model.stat().st_size,
                "summary": {
                    "monolingual_mean_wer": round(monolingual_wer, 4),
                    "mixed_mean_wer": (
                        None if mixed_wer is None else round(mixed_wer, 4)
                    ),
                    "language_accuracy": round(language_accuracy, 4),
                    "fast_lane_intent_accuracy": (
                        None if intent_accuracy is None else round(intent_accuracy, 4)
                    ),
                    "median_latency_milliseconds": round(statistics.median(latencies)),
                    "p95_latency_milliseconds": p95_latency,
                    "median_real_time_factor": round(
                        statistics.median(
                            attempt["real_time_factor"] for attempt in flat
                        ),
                        4,
                    ),
                    "passes": (
                        monolingual_wer <= arguments.monolingual_wer_target
                        and (
                            mixed_wer is None
                            or mixed_wer <= arguments.mixed_wer_target
                        )
                        and language_accuracy == 1
                        and (
                            intent_accuracy is None
                            or intent_accuracy >= arguments.intent_accuracy_target
                        )
                        and p95_latency <= arguments.latency_target_ms
                    ),
                },
                "cases": model_cases,
            }
        )
    return {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "settings": {
            "threads": arguments.threads,
            "repeats": arguments.repeats,
            "monolingual_wer_target": arguments.monolingual_wer_target,
            "mixed_wer_target": arguments.mixed_wer_target,
            "latency_target_milliseconds": arguments.latency_target_ms,
            "intent_accuracy_target": arguments.intent_accuracy_target,
            "wake_phrase": arguments.wake_phrase,
            "prompt": arguments.prompt,
            "offline": True,
        },
        "models": results,
    }


def main() -> int:
    arguments = parser().parse_args()
    if arguments.repeats < 1:
        raise SystemExit("--repeats must be positive")
    payload = benchmark(arguments)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
