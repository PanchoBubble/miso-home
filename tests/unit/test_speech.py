from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import sys
import unittest

from miso.audio import AudioFormat
from miso.speech import (
    PiperBackend,
    PiperVoice,
    SpeechError,
    SpeechManager,
    SynthesisMetrics,
)


class FakeAudio:
    playback_format = AudioFormat(sample_rate=22_050)

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.cancelled = 0

    def play_stream(self, pcm, timeout=1.0, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelled")
        self.chunks.append(pcm)

    def cancel_playback(self):
        self.cancelled += 1

    def wait_playback(self, timeout):
        return True


class FakeBackend:
    def __init__(self, *, block=False) -> None:
        self.voice = PiperVoice("en", "test-en", Path("en"), Path("en.json"))
        self.voices = {"en": self.voice}
        self.block = block
        self.volume = None

    def available(self):
        return True

    def synthesize(self, text, language, volume, cancel_event, on_audio):
        if language not in self.voices:
            raise SpeechError("speech language must be en or es")
        self.volume = volume
        started = time.monotonic()
        chunks = 0
        while self.block and not cancel_event.wait(0.01):
            on_audio(b"\x01\x00" * 20)
            chunks += 1
        if not self.block:
            on_audio(b"\x01\x00" * 20)
            chunks = 1
        return SynthesisMetrics(
            self.voice,
            5,
            round((time.monotonic() - started) * 1000),
            20,
            chunks,
            cancel_event.is_set(),
        )


class PiperBackendTests(unittest.TestCase):
    def test_streams_even_pcm_chunks_with_selected_voice_and_volume(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker.py"
            worker.write_text(
                "#!/usr/bin/env python3\n"
                "import json, struct, sys\n"
                "out = sys.stdout.buffer; inp = sys.stdin.buffer\n"
                "out.write(struct.pack('>I', 0xffffffff)); out.flush()\n"
                "size = struct.unpack('>I', inp.read(4))[0]\n"
                "request = json.loads(inp.read(size))\n"
                "assert request['text'] == 'Buenos días'\n"
                "pcm = b'\\x01\\x00' * 3000\n"
                "out.write(struct.pack('>I', len(pcm)) + pcm + struct.pack('>I', 0))\n"
                "out.flush()\n",
                encoding="utf-8",
            )
            model = root / "voice.onnx"
            config = root / "voice.onnx.json"
            model.touch()
            config.touch()
            voice = PiperVoice("es", "test-es", model, config)
            backend = PiperBackend(
                Path(sys.executable),
                (voice,),
                chunk_bytes=1024,
                timeout_seconds=2,
                worker=worker,
            )
            self.assertTrue(backend.available())
            chunks: list[bytes] = []
            result = backend.synthesize(
                "Buenos días", "es", 0.75, threading.Event(), chunks.append
            )
            backend.stop()

        self.assertEqual(b"".join(chunks), b"\x01\x00" * 3000)
        self.assertTrue(all(len(chunk) % 2 == 0 for chunk in chunks))
        self.assertEqual(result.voice.name, "test-es")
        self.assertEqual(result.audio_milliseconds, 136)
        self.assertIsNotNone(result.first_audio_milliseconds)

    def test_rejects_unknown_language(self) -> None:
        backend = PiperBackend(Path("missing"), (), chunk_bytes=1024, timeout_seconds=1)
        with self.assertRaisesRegex(SpeechError, "en or es"):
            backend.synthesize("hello", "fr", 1, threading.Event(), lambda _: None)


class SpeechManagerTests(unittest.TestCase):
    def test_streams_completes_and_redacts_text_from_status(self) -> None:
        audio = FakeAudio()
        backend = FakeBackend()
        manager = SpeechManager(
            enabled=True,
            backend=backend,
            audio=audio,
            default_volume=1,
            result_capacity=2,
        )
        request_id = manager.speak("private household response", "en", volume=0.6)
        result = manager.wait(request_id, 1)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "completed")
        self.assertEqual(backend.volume, 0.6)
        self.assertTrue(audio.chunks)
        self.assertNotIn("private household response", str(manager.status()))

    def test_cancellation_stops_synthesis_and_playback_promptly(self) -> None:
        audio = FakeAudio()
        manager = SpeechManager(
            enabled=True,
            backend=FakeBackend(block=True),
            audio=audio,
            default_volume=1,
            result_capacity=2,
        )
        request_id = manager.speak("long response", "en")
        deadline = time.monotonic() + 1
        while not audio.chunks and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        self.assertTrue(manager.cancel(request_id))
        result = manager.wait(request_id, 1)

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "cancelled")
        self.assertLess((time.monotonic() - started) * 1000, 100)
        self.assertGreaterEqual(audio.cancelled, 1)

    def test_validates_input_and_disabled_state(self) -> None:
        manager = SpeechManager(
            enabled=False,
            backend=FakeBackend(),
            audio=FakeAudio(),
            default_volume=1,
            result_capacity=2,
        )
        with self.assertRaisesRegex(SpeechError, "disabled"):
            manager.speak("hello", "en")


if __name__ == "__main__":
    unittest.main()
