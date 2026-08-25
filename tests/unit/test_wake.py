from array import array
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from miso.audio import AudioFormat, BoundedPCMBuffer
from miso.wake import OpenWakeWordModel, WakeDetector, WakeWordManager


def frame(amplitude: int = 4_000) -> bytes:
    return array("h", [amplitude, -amplitude] * 640).tobytes()


class FakeWakeModel:
    model = Path("/models/miso.onnx")

    def __init__(self, scores: list[float], available: bool = True) -> None:
        self.scores = scores
        self.is_available = available
        self.reset_count = 0
        self.closed = 0

    def available(self) -> bool:
        return self.is_available

    def score(self, _pcm: bytes) -> float:
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self) -> None:
        self.reset_count += 1

    def close(self) -> None:
        self.closed += 1


class FakeCaptureBus:
    audio_format = AudioFormat()

    def __init__(self) -> None:
        self.buffer: BoundedPCMBuffer | None = None
        self.unsubscribed = False

    def subscribe_capture(self, capacity: int | None = None) -> BoundedPCMBuffer:
        self.buffer = BoundedPCMBuffer(capacity or 8)
        return self.buffer

    def unsubscribe_capture(self, _subscriber: BoundedPCMBuffer) -> None:
        self.unsubscribed = True


class WakeDetectorTests(unittest.TestCase):
    def test_requires_speech_and_consecutive_scores_then_applies_cooldown(self) -> None:
        model = FakeWakeModel([0.99, 0.8, 0.9, 0.9, 0.9])
        detector = WakeDetector(
            model,
            AudioFormat(),
            phrase="Miso",
            threshold=0.7,
            energy_threshold_dbfs=-45,
            activation_frames=2,
            cooldown_seconds=2,
        )

        self.assertEqual(detector.feed(frame(0), now=10), ())
        self.assertEqual(detector.feed(frame(), now=10), ())
        first = detector.feed(frame(), now=10)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].phrase, "Miso")
        self.assertAlmostEqual(first[0].score, 0.9)
        self.assertEqual(detector.feed(frame() + frame(), now=11), ())

        detector.reset()
        self.assertEqual(model.reset_count, 1)
        self.assertEqual(detector.highest_score, 0)

    def test_buffers_partial_capture_chunks_into_1280_sample_frames(self) -> None:
        model = FakeWakeModel([0.9])
        detector = WakeDetector(
            model,
            AudioFormat(chunk_milliseconds=20),
            phrase="Miso",
            threshold=0.5,
            energy_threshold_dbfs=-45,
            activation_frames=1,
            cooldown_seconds=0,
        )
        chunk = frame()
        self.assertEqual(detector.feed(chunk[:640]), ())
        self.assertEqual(detector.feed(chunk[640:1280]), ())
        self.assertEqual(len(detector.feed(chunk[1280:])), 1)

    def test_rejects_incompatible_audio_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "mono 16000"):
            WakeDetector(
                FakeWakeModel([]),
                AudioFormat(sample_rate=22_050),
                phrase="Miso",
                threshold=0.5,
                energy_threshold_dbfs=-45,
                activation_frames=1,
                cooldown_seconds=0,
            )

    def test_rejects_partial_pcm_sample(self) -> None:
        detector = WakeDetector(
            FakeWakeModel([]),
            AudioFormat(),
            phrase="Miso",
            threshold=0.5,
            energy_threshold_dbfs=-45,
            activation_frames=1,
            cooldown_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "complete S16_LE"):
            detector.feed(b"odd")


class OpenWakeWordModelTests(unittest.TestCase):
    def test_persistent_worker_protocol_scores_resets_and_stops(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "miso"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "openwakeword_worker.py").write_text(
                "import struct, sys\n"
                "sys.stdout.buffer.write(b'OWW1'); sys.stdout.buffer.flush()\n"
                "while True:\n"
                " h=sys.stdin.buffer.read(4)\n"
                " if not h: break\n"
                " n=struct.unpack('>I',h)[0]\n"
                " if n != 0xffffffff: sys.stdin.buffer.read(n)\n"
                " sys.stdout.buffer.write(struct.pack('>Bf',0,0.75)); "
                "sys.stdout.buffer.flush()\n",
                encoding="utf-8",
            )
            model_path = root / "miso.onnx"
            model_path.touch()
            previous = os.environ.get("PYTHONPATH")
            os.environ["PYTHONPATH"] = os.pathsep.join(
                value for value in (str(root), previous) if value
            )
            try:
                model = OpenWakeWordModel(
                    Path(sys.executable), model_path, vad_threshold=0.5
                )
                self.assertTrue(model.available())
                self.assertAlmostEqual(model.score(frame()), 0.75)
                model.reset()
                model.close()
                self.assertIsNone(model._process)
            finally:
                if previous is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = previous


class WakeManagerTests(unittest.TestCase):
    def test_publishes_bounded_events_and_redacted_status(self) -> None:
        audio = FakeCaptureBus()
        model = FakeWakeModel([0.95])
        activations = []
        manager = WakeWordManager(
            enabled=True,
            audio=audio,
            model=model,
            phrase="Miso",
            threshold=0.5,
            energy_threshold_dbfs=-45,
            activation_frames=1,
            cooldown_seconds=0,
            result_capacity=2,
            on_activation=activations.append,
        )
        manager.start()
        assert audio.buffer is not None
        audio.buffer.put(frame())
        deadline = time.monotonic() + 1
        while manager.status()["activations"] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        event = manager.get_event(timeout=0.1)
        manager.stop()

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.phrase, "Miso")
        self.assertEqual(activations, [event])
        status = manager.status()
        self.assertEqual(status["model"], "miso.onnx")
        self.assertNotIn("/models", str(status))
        self.assertEqual(status["highest_score"], 0.95)
        self.assertTrue(audio.unsubscribed)
        self.assertEqual(model.closed, 1)

    def test_reports_missing_offline_model_without_processing_audio(self) -> None:
        audio = FakeCaptureBus()
        manager = WakeWordManager(
            enabled=True,
            audio=audio,
            model=FakeWakeModel([], available=False),
            phrase="Miso",
            threshold=0.5,
            energy_threshold_dbfs=-45,
            activation_frames=1,
            cooldown_seconds=0,
            result_capacity=2,
        )
        manager.start()
        deadline = time.monotonic() + 0.5
        while manager.status()["state"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(manager.status()["state"], "unavailable")
        manager.stop()


if __name__ == "__main__":
    unittest.main()
