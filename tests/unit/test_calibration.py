from array import array
from pathlib import Path
import unittest

from miso.audio import AudioFormat, BoundedPCMBuffer
from miso.calibration import WakeCalibration, WakeCalibrationComplete
from miso.transcription import TranscriptionResult, Utterance


class FakeAudio:
    audio_format = AudioFormat()

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.unsubscribed = False

    def subscribe_capture(self, capacity=None):
        buffer = BoundedPCMBuffer(capacity or 10)
        for chunk in self.chunks:
            buffer.put(chunk)
        return buffer

    def unsubscribe_capture(self, _subscriber):
        self.unsubscribed = True


class FakeTranscriber:
    model = Path("/models/whisper.bin")

    def __init__(self) -> None:
        self.utterance: Utterance | None = None

    def available(self) -> bool:
        return True

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        self.utterance = utterance
        return TranscriptionResult(
            text="Me so.",
            language="en",
            model_language="en",
            language_confidence=0.9,
            confidence=0.8,
            segments=(),
            audio_milliseconds=utterance.duration_milliseconds,
            inference_milliseconds=10,
            real_time_factor=0.1,
            model="fake.bin",
        )


class WakeCalibrationTests(unittest.TestCase):
    def test_captures_transcribes_and_allows_only_one_completed_clip(self) -> None:
        samples = array("h", [4_000, -4_000] * 320).tobytes()
        audio = FakeAudio([samples, samples])
        transcriber = FakeTranscriber()
        calibration = WakeCalibration(
            enabled=True,
            audio=audio,
            transcriber=transcriber,
            duration_seconds=0.04,
        )

        result = calibration.capture()

        self.assertEqual(result["recognized_text"], "Me so.")
        self.assertEqual(result["duration_milliseconds"], 40)
        self.assertFalse(result["raw_audio_retained"])
        self.assertTrue(audio.unsubscribed)
        self.assertIsNotNone(transcriber.utterance)
        with self.assertRaises(WakeCalibrationComplete):
            calibration.capture()


if __name__ == "__main__":
    unittest.main()
