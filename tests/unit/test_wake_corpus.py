import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

from miso.wake_corpus import load_wake_corpus


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\0\0" * 1_280)


class WakeCorpusTests(unittest.TestCase):
    def test_loads_disjoint_training_and_evaluation_cases(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("train.wav", "evaluation.wav", "room.wav"):
                write_wav(root / name)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "consent": {
                            "confirmed": True,
                            "confirmed_at": "2020-01-01T00:00:00Z",
                            "delete_raw_by": "2099-01-02T00:00:00Z",
                        },
                        "cases": [
                            {
                                "path": "train.wav",
                                "label": "positive",
                                "language": "en",
                                "distance_meters": 1,
                                "split": "training",
                                "group_id": "speaker-a",
                            },
                            {
                                "path": "evaluation.wav",
                                "label": "positive",
                                "language": "es",
                                "distance_meters": 3,
                                "split": "evaluation",
                                "group_id": "speaker-b",
                            },
                            {
                                "path": "room.wav",
                                "label": "negative",
                                "split": "evaluation",
                                "group_id": "room-session-b",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            corpus = load_wake_corpus(manifest)

            self.assertEqual(len(corpus.cases_for("training", "positive")), 1)
            self.assertEqual(len(corpus.cases_for("evaluation")), 2)

    def test_rejects_group_leakage_between_splits(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "one.wav")
            write_wav(root / "two.wav")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "consent": {
                            "confirmed": True,
                            "confirmed_at": "2020-01-01T00:00:00Z",
                            "delete_raw_by": "2099-01-02T00:00:00Z",
                        },
                        "cases": [
                            {
                                "path": "one.wav",
                                "label": "positive",
                                "language": "en",
                                "split": "training",
                                "group_id": "speaker-a",
                            },
                            {
                                "path": "two.wav",
                                "label": "positive",
                                "language": "en",
                                "split": "evaluation",
                                "group_id": "speaker-a",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "both training and evaluation"):
                load_wake_corpus(manifest)

    def test_rejects_paths_outside_manifest_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / f"{root.name}-outside.wav"
            write_wav(outside)
            self.addCleanup(outside.unlink, missing_ok=True)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "consent": {
                            "confirmed": True,
                            "confirmed_at": "2020-01-01T00:00:00Z",
                            "delete_raw_by": "2099-01-02T00:00:00Z",
                        },
                        "cases": [
                            {
                                "path": f"../{outside.name}",
                                "label": "negative",
                                "split": "training",
                                "group_id": "room-a",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "escapes"):
                load_wake_corpus(manifest)

    def test_required_split_allows_already_deleted_other_split(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_wav(root / "train.wav")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "consent": {
                            "confirmed": True,
                            "confirmed_at": "2020-01-01T00:00:00Z",
                            "delete_raw_by": "2099-01-02T00:00:00Z",
                        },
                        "cases": [
                            {
                                "path": "train.wav",
                                "label": "negative",
                                "split": "training",
                                "group_id": "room-a",
                            },
                            {
                                "path": "deleted-evaluation.wav",
                                "label": "negative",
                                "split": "evaluation",
                                "group_id": "room-b",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            corpus = load_wake_corpus(manifest, required_split="training")

            self.assertEqual(len(corpus.cases), 2)
            with self.assertRaisesRegex(ValueError, "does not exist"):
                load_wake_corpus(manifest)

    def test_deletion_helper_removes_only_selected_split(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            train = root / "train.wav"
            evaluation = root / "evaluation.wav"
            write_wav(train)
            write_wav(evaluation)
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "consent": {
                            "confirmed": True,
                            "confirmed_at": "2020-01-01T00:00:00Z",
                            "delete_raw_by": "2099-01-02T00:00:00Z",
                        },
                        "cases": [
                            {
                                "path": train.name,
                                "label": "negative",
                                "split": "training",
                                "group_id": "room-a",
                            },
                            {
                                "path": evaluation.name,
                                "label": "negative",
                                "split": "evaluation",
                                "group_id": "room-b",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            receipt = root / "receipt.json"
            project = Path(__file__).resolve().parents[2]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(project / "src")

            subprocess.run(
                [
                    sys.executable,
                    str(project / "ops" / "delete-wakeword-corpus.py"),
                    "--manifest",
                    str(manifest),
                    "--split",
                    "training",
                    "--audit-output",
                    str(receipt),
                    "--confirm-delete",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertFalse(train.exists())
            self.assertTrue(evaluation.exists())
            audit = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(audit["deleted_files"], 1)
            self.assertFalse(audit["raw_audio_retained"])


if __name__ == "__main__":
    unittest.main()
