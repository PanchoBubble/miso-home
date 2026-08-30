#!/usr/bin/env python3
"""Synthesize the bilingual timer fixtures used by ops/benchmark-whisper.py.

The audio is generated rather than committed: macOS system voices are
deterministic, so the same command reproduces byte-identical WAVs on any Mac
and the repository stays free of binary fixtures. Recorded human speech is a
separate, stricter gate.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


CASES: tuple[dict[str, object], ...] = (
    {
        "id": "es-timer-five-seconds",
        "voice": "Mónica",
        "language": "es",
        "text": "Miso, pon un temporizador de cinco segundos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 5},
    },
    {
        "id": "es-timer-thirty-seconds",
        "voice": "Mónica",
        "language": "es",
        "text": "Pon un temporizador de treinta segundos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 30},
    },
    {
        "id": "es-timer-two-minutes",
        "voice": "Mónica",
        "language": "es",
        "text": "Miso, pon un temporizador de dos minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 120},
    },
    {
        "id": "es-timer-ten-minutes",
        "voice": "Mónica",
        "language": "es",
        "text": "Pon un temporizador de diez minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 600},
    },
    {
        "id": "es-timer-one-hour",
        "voice": "Mónica",
        "language": "es",
        "text": "Pon un temporizador de una hora",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 3600},
    },
    {
        "id": "es-mx-timer-five-seconds",
        "voice": "Paulina",
        "language": "es",
        "text": "Miso, pon un temporizador de cinco segundos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 5},
    },
    {
        "id": "es-mx-timer-fifteen-minutes",
        "voice": "Paulina",
        "language": "es",
        "text": "Pon un temporizador de quince minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 900},
    },
    {
        "id": "es-mx-timer-twenty-minutes",
        "voice": "Paulina",
        "language": "es",
        "text": "Miso, pon un temporizador de veinte minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 1200},
    },
    {
        "id": "es-timer-remaining",
        "voice": "Mónica",
        "language": "es",
        "text": "Miso, cuánto queda en el temporizador",
        "intent": "timer_list",
        "intent_arguments": {"status": "pending"},
    },
    {
        "id": "es-mx-timer-three-minutes",
        "voice": "Paulina",
        "language": "es",
        "text": "Pon un temporizador de tres minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 180},
    },
    {
        "id": "es-timer-five-minutes",
        "voice": "Mónica",
        "language": "es",
        "text": "Miso, pon un temporizador de cinco minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 300},
    },
    {
        "id": "es-timer-forty-seconds",
        "voice": "Mónica",
        "language": "es",
        "text": "Pon un temporizador de cuarenta segundos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 40},
    },
    {
        "id": "es-timer-nine-minutes",
        "voice": "Mónica",
        "language": "es",
        "text": "Miso, pon un temporizador de nueve minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 540},
    },
    {
        "id": "es-stopwatch-thirty-seconds",
        "voice": "Mónica",
        "language": "es",
        "text": "Pon un cronómetro de treinta segundos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 30},
    },
    {
        "id": "es-mx-timer-six-minutes",
        "voice": "Paulina",
        "language": "es",
        "text": "Pon un temporizador de seis minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 360},
    },
    {
        "id": "es-mx-timer-twelve-minutes",
        "voice": "Paulina",
        "language": "es",
        "text": "Pon un temporizador de doce minutos",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 720},
    },
    {
        "id": "es-mx-timer-missing",
        "voice": "Paulina",
        "language": "es",
        "text": "Miso, cuánto falta en el temporizador",
        "intent": "timer_list",
        "intent_arguments": {"status": "pending"},
    },
    # English controls: a prompt tuned for Spanish must not cost English.
    {
        "id": "en-timer-five-minutes",
        "voice": "Samantha",
        "language": "en",
        "text": "Miso, set a timer for five minutes",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 300},
    },
    {
        "id": "en-timer-ten-minutes",
        "voice": "Samantha",
        "language": "en",
        "text": "Miso, set a timer for ten minutes",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 600},
    },
    {
        "id": "en-gb-timer-thirty-seconds",
        "voice": "Daniel",
        "language": "en",
        "text": "Set a timer for thirty seconds",
        "intent": "timer_create",
        "intent_arguments": {"duration_seconds": 30},
    },
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--output-dir", type=Path, required=True)
    result.add_argument("--sample-rate", type=int, default=16_000)
    return result


def synthesize(case: dict[str, object], directory: Path, sample_rate: int) -> str:
    name = f"{case['id']}.wav"
    subprocess.run(
        [
            "say",
            "-v",
            str(case["voice"]),
            "-o",
            str(directory / name),
            "--file-format=WAVE",
            f"--data-format=LEI16@{sample_rate}",
            "--channels=1",
            str(case["text"]),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return name


def main() -> int:
    arguments = parser().parse_args()
    if sys.platform != "darwin":
        raise SystemExit("these fixtures require the macOS say(1) voices")
    directory = arguments.output_dir
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    for case in CASES:
        audio = synthesize(case, directory, arguments.sample_rate)
        manifest.append(
            {
                "id": case["id"],
                "audio": audio,
                "language": case["language"],
                "text": case["text"],
                "category": "monolingual",
                "voice": case["voice"],
                "intent": case["intent"],
                "intent_arguments": case["intent_arguments"],
            }
        )
    path = directory / "manifest.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"{len(manifest)} fixtures written to {directory}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
