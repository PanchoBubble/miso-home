from array import array
from collections import deque
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from miso.audio import AudioFormat
from miso.transcription import (
    EnergySpeechDetector,
    TranscriptionManager,
    TranscriptionResult,
    Utterance,
    UtteranceAssembler,
    WhisperCppTranscriber,
    normalized_words,
    word_error_rate,
)


def pcm(value: int, milliseconds: int = 20) -> bytes:
    return array("h", [value] * (16 * milliseconds)).tobytes()


class UtteranceAssemblerTests(unittest.TestCase):
    def test_emits_after_vad_end_with_pre_roll(self) -> None:
        audio_format = AudioFormat(chunk_milliseconds=20)
        assembler = UtteranceAssembler(
            audio_format,
            minimum_speech_milliseconds=40,
            end_silence_milliseconds=40,
            maximum_utterance_milliseconds=1_000,
            pre_roll_milliseconds=20,
        )
        self.assertIsNone(assembler.feed(pcm(0), speech=False))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(0), speech=False))
        utterance = assembler.feed(pcm(0), speech=False)

        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertEqual(utterance.duration_milliseconds, 100)
        self.assertFalse(utterance.truncated)
        self.assertEqual(len(utterance.pcm), audio_format.chunk_bytes * 5)

    def test_discards_short_noise_and_bounds_long_speech(self) -> None:
        assembler = UtteranceAssembler(
            AudioFormat(chunk_milliseconds=20),
            minimum_speech_milliseconds=40,
            end_silence_milliseconds=20,
            maximum_utterance_milliseconds=60,
            pre_roll_milliseconds=0,
        )
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(0), speech=False))

        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        self.assertIsNone(assembler.feed(pcm(4_000), speech=True))
        utterance = assembler.feed(pcm(4_000), speech=True)
        self.assertIsNotNone(utterance)
        assert utterance is not None
        self.assertTrue(utterance.truncated)
        self.assertEqual(utterance.duration_milliseconds, 60)

    def test_energy_detector_distinguishes_silence(self) -> None:
        detector = EnergySpeechDetector(-40)
        self.assertFalse(detector.is_speech(pcm(0)))
        self.assertTrue(detector.is_speech(pcm(4_000)))


class WhisperCppTranscriberTests(unittest.TestCase):
    def test_returns_language_timestamps_and_confidence(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "ggml-base-q5_1.bin"
            model.write_bytes(b"model")
            executable = root / "whisper-cli"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, pathlib, sys\n"
                "prefix = pathlib.Path(sys.argv[sys.argv.index('--output-file') + 1])\n"
                "payload = {\n"
                " 'result': {'language': 'es'},\n"
                " 'transcription': [{\n"
                "  'offsets': {'from': 120, 'to': 980},\n"
                "  'text': ' apaga la luz',\n"
                "  'tokens': [\n"
                "   {'text': '<|startoftranscript|>', 'p': 0.99},\n"
                "   {'text': ' apaga', 'p': 0.8, 'offsets': {'from': 120, 'to': 500}},\n"
                "   {'text': ' la luz', 'p': 0.6, 'offsets': {'from': 500, 'to': 980}}\n"
                "  ]\n"
                " }]\n"
                "}\n"
                "prefix.with_suffix('.json').write_text(json.dumps(payload))\n"
                "print('whisper_full_with_state: auto-detected language: es (p = 0.91)', file=sys.stderr)\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            transcriber = WhisperCppTranscriber(
                executable, model, threads=4, timeout_seconds=5
            )
            result = transcriber.transcribe(
                Utterance(pcm(2_000, 1_000), 16_000, 1, 1_000)
            )

        self.assertEqual(result.text, "apaga la luz")
        self.assertEqual(result.language, "es")
        self.assertEqual(result.model_language, "es")
        self.assertAlmostEqual(result.language_confidence or 0, 0.91)
        self.assertAlmostEqual(result.confidence or 0, 0.7)
        self.assertEqual(result.segments[0].start_milliseconds, 120)
        self.assertEqual(result.segments[0].tokens[1].end_milliseconds, 980)
        self.assertEqual(result.model, "ggml-base-q5_1.bin")
        self.assertGreaterEqual(result.inference_milliseconds, 0)

    def test_rejects_missing_runtime(self) -> None:
        transcriber = WhisperCppTranscriber(
            Path("/missing/whisper-cli"),
            Path("/missing/model.bin"),
            threads=1,
            timeout_seconds=1,
        )
        self.assertFalse(transcriber.available())
        with self.assertRaisesRegex(RuntimeError, "executable is unavailable"):
            transcriber.transcribe(Utterance(b"\0\0", 16_000, 1, 1))

    def test_classifies_code_switch_and_preserves_acoustic_language(self) -> None:
        transcriber = WhisperCppTranscriber(
            Path("/bin/whisper-cli"),
            Path("/models/ggml-tiny.bin"),
            threads=4,
            timeout_seconds=5,
        )
        payload = {
            "result": {"language": "es"},
            "transcription": [
                {
                    "offsets": {"from": 0, "to": 1_000},
                    "text": "set a timer for five minutes y apaga la luz",
                    "tokens": [
                        {"text": " set a timer", "p": 0.8},
                        {"text": " y apaga la luz", "p": 0.7},
                    ],
                }
            ],
        }
        result = transcriber._result(
            payload,
            "auto-detected language: es (p = 0.62)",
            Utterance(pcm(1_000, 1_000), 16_000, 1, 1_000),
            500,
        )

        self.assertEqual(result.language, "mixed")
        self.assertEqual(result.model_language, "es")
        self.assertEqual(result.language_confidence, 0.62)


class FakeAudio:
    audio_format = AudioFormat(chunk_milliseconds=20)

    def __init__(self) -> None:
        self.chunks = deque((pcm(4_000), pcm(4_000), pcm(0), pcm(0)))

    def read_capture(self, timeout: float | None = None) -> bytes | None:
        if self.chunks:
            return self.chunks.popleft()
        time.sleep(min(timeout or 0, 0.01))
        return None


class FakeTranscriber:
    model = Path("/models/ggml-tiny.bin")

    def available(self) -> bool:
        return True

    def transcribe(self, utterance: Utterance) -> TranscriptionResult:
        return TranscriptionResult(
            text="enciende la luz",
            language="es",
            model_language="es",
            language_confidence=0.95,
            confidence=0.9,
            segments=(),
            audio_milliseconds=utterance.duration_milliseconds,
            inference_milliseconds=100,
            real_time_factor=1.0,
            model=self.model.name,
        )


class TranscriptionManagerTests(unittest.TestCase):
    def test_processes_post_vad_utterance_and_reports_bounded_status(self) -> None:
        audio = FakeAudio()
        manager = TranscriptionManager(
            enabled=True,
            audio=audio,
            transcriber=FakeTranscriber(),
            detector=EnergySpeechDetector(-40),
            assembler=UtteranceAssembler(
                audio.audio_format,
                minimum_speech_milliseconds=40,
                end_silence_milliseconds=40,
                maximum_utterance_milliseconds=1_000,
                pre_roll_milliseconds=0,
            ),
            result_capacity=2,
        )
        manager.start()
        result = manager.get_result(timeout=1)
        manager.stop()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.text, "enciende la luz")
        status = manager.status()
        self.assertEqual(status["processed"], 1)
        self.assertNotIn("enciende", json.dumps(status))


class BenchmarkMetricTests(unittest.TestCase):
    def test_normalizes_accents_and_calculates_wer(self) -> None:
        self.assertEqual(
            normalized_words("¡Enciende la lámpara!"),
            ("enciende", "la", "lampara"),
        )
        self.assertEqual(word_error_rate("set a timer", "set the timer"), 1 / 3)
        self.assertEqual(
            word_error_rate("pon un temporizador", "pon un temporizador"), 0
        )

    def test_fixture_shape_is_serializable(self) -> None:
        result = {"wer": word_error_rate("hello", "hello")}
        self.assertEqual(json.loads(json.dumps(result)), {"wer": 0.0})


if __name__ == "__main__":
    unittest.main()
