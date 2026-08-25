#!/usr/bin/env python3
"""Measure Miso wake-word recall and false activations on labeled WAV files."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

from miso.audio import AudioFormat
from miso.wake import OpenWakeWordModel, WakeDetector, WakeWordError
from miso.wake_corpus import load_wake_corpus


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--manifest-split",
        choices=("training", "evaluation"),
        default="evaluation",
    )
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--executable",
        type=Path,
        default=Path("/opt/miso/openwakeword/bin/python"),
    )
    parser.add_argument("--phrase", default="Miso")
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--energy-threshold-dbfs", type=float, default=-45)
    parser.add_argument("--activation-frames", type=int, default=2)
    parser.add_argument("--cooldown-seconds", type=float, default=2)
    parser.add_argument("--minimum-recall", type=float, default=0.8)
    parser.add_argument("--maximum-false-activations-per-hour", type=float, default=0.5)
    parser.add_argument("--target-distance-meters", type=float, default=3.0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _audio(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as source:
        if (
            source.getframerate() != 16_000
            or source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getcomptype() != "NONE"
        ):
            raise ValueError(f"{path} must be uncompressed mono 16 kHz 16-bit WAV")
        frames = source.readframes(source.getnframes())
        return frames, source.getnframes() / source.getframerate()


def benchmark(options: argparse.Namespace) -> dict[str, object]:
    corpus = load_wake_corpus(
        options.manifest, required_split=options.manifest_split
    )
    cases = corpus.cases_for(options.manifest_split)
    if not cases:
        raise ValueError(f"manifest has no {options.manifest_split} cases")
    model = OpenWakeWordModel(
        options.executable,
        options.model,
        vad_threshold=options.vad_threshold,
    )
    detector = WakeDetector(
        model,
        AudioFormat(),
        phrase=options.phrase,
        threshold=options.threshold,
        energy_threshold_dbfs=options.energy_threshold_dbfs,
        activation_frames=options.activation_frames,
        cooldown_seconds=options.cooldown_seconds,
    )
    results: list[dict[str, object]] = []
    positives = true_positives = false_positive_files = false_activations = 0
    negative_seconds = 0.0
    target_positives = target_true_positives = 0
    language_counts: dict[str, list[int]] = {"en": [0, 0], "es": [0, 0]}
    try:
        for case in cases:
            relative = case.relative_path
            label = case.label
            path = case.path
            pcm, duration_seconds = _audio(path)
            detector.reset()
            events = []
            chunk_bytes = AudioFormat().chunk_bytes
            for offset in range(0, len(pcm), chunk_bytes):
                events.extend(
                    detector.feed(
                        pcm[offset : offset + chunk_bytes],
                        now=offset / (16_000 * 2),
                    )
                )
            detected = bool(events)
            if label == "positive":
                positives += 1
                true_positives += int(detected)
                language = case.language
                if language in language_counts:
                    language_counts[language][0] += 1
                    language_counts[language][1] += int(detected)
                distance = case.distance_meters
                if (
                    isinstance(distance, (int, float))
                    and distance >= options.target_distance_meters
                ):
                    target_positives += 1
                    target_true_positives += int(detected)
            else:
                negative_seconds += duration_seconds
                false_positive_files += int(detected)
                false_activations += len(events)
            results.append(
                {
                    "path": relative,
                    "label": label,
                    "language": case.language,
                    "distance_meters": case.distance_meters,
                    "duration_seconds": round(duration_seconds, 3),
                    "detected": detected,
                    "activations": len(events),
                    "highest_score": round(detector.highest_score, 4),
                }
            )
    finally:
        model.close()

    recall = true_positives / positives if positives else 0.0
    target_recall = (
        target_true_positives / target_positives if target_positives else None
    )
    false_activations_per_hour = (
        false_activations / (negative_seconds / 3600) if negative_seconds else 0.0
    )
    passed = (
        positives > 0
        and recall >= options.minimum_recall
        and all(
            total > 0 and detected / total >= options.minimum_recall
            for total, detected in language_counts.values()
        )
        and target_positives > 0
        and target_recall is not None
        and target_recall >= options.minimum_recall
        and negative_seconds >= 3600
        and false_activations_per_hour <= options.maximum_false_activations_per_hour
    )
    return {
        "phrase": options.phrase,
        "model": options.model.name,
        "offline": True,
        "corpus": {
            "split": options.manifest_split,
            "consent_confirmed_at": corpus.consent_confirmed_at,
            "delete_raw_by": corpus.delete_raw_by,
        },
        "settings": {
            "threshold": options.threshold,
            "vad_threshold": options.vad_threshold,
            "energy_threshold_dbfs": options.energy_threshold_dbfs,
            "activation_frames": options.activation_frames,
            "cooldown_seconds": options.cooldown_seconds,
            "target_distance_meters": options.target_distance_meters,
        },
        "summary": {
            "positive_cases": positives,
            "true_positives": true_positives,
            "misses": positives - true_positives,
            "recall": round(recall, 4),
            "language_recall": {
                language: (None if total == 0 else round(detected / total, 4))
                for language, (total, detected) in language_counts.items()
            },
            "target_distance_positive_cases": target_positives,
            "target_distance_recall": (
                None if target_recall is None else round(target_recall, 4)
            ),
            "negative_files": len(cases) - positives,
            "false_positive_files": false_positive_files,
            "negative_audio_hours": round(negative_seconds / 3600, 6),
            "false_activations": false_activations,
            "false_activations_per_hour": round(false_activations_per_hour, 4),
            "passed": passed,
        },
        "cases": results,
    }


def main() -> int:
    options = _arguments()
    try:
        result = benchmark(options)
    except (OSError, ValueError, json.JSONDecodeError, WakeWordError) as error:
        print(f"wake benchmark error: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if options.output is not None:
        options.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
