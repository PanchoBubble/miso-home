from array import array
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from miso.audio import (
    ALSABackend,
    AudioDevice,
    AudioFormat,
    AudioManager,
    AudioSelector,
    BoundedPCMBuffer,
    PCMLevelMeter,
    discover_alsa_devices,
    select_audio_device,
)


def pcm(*samples: int) -> bytes:
    values = array("h", samples)
    return values.tobytes()


class AudioDiscoveryTests(unittest.TestCase):
    def test_discovers_capabilities_and_selects_stable_card_after_renumber(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cards").write_text(
                " 1 [HDMI           ]: vc4-hdmi - HDMI\n"
                " 3 [MisoUSB        ]: USB-Audio - Miso USB Array\n",
                encoding="utf-8",
            )
            (root / "pcm").write_text(
                "01-00: MAI PCM : MAI PCM : playback 1\n"
                "03-00: USB Audio : USB Audio : playback 1 : capture 1\n",
                encoding="utf-8",
            )
            devices = discover_alsa_devices(root)

        self.assertEqual(len(devices), 2)
        selected = select_audio_device(
            devices, AudioSelector("MisoUSB", 0), direction="capture"
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.card_index, 3)
        self.assertEqual(selected.pcm_name, "plughw:CARD=MisoUSB,DEV=0")
        self.assertTrue(selected.capture)
        self.assertTrue(selected.playback)
        self.assertIsNone(
            select_audio_device(
                devices, AudioSelector("HDMI", 0), direction="capture"
            )
        )

    def test_missing_proc_inventory_is_empty(self) -> None:
        self.assertEqual(discover_alsa_devices(Path("/definitely/missing")), ())

    def test_alsa_command_uses_stable_selector_and_raw_pcm(self) -> None:
        device = AudioDevice("MisoUSB", 7, 0, "USB Audio", True, True)
        command = ALSABackend._command("arecord", device, AudioFormat())
        self.assertIn("plughw:CARD=MisoUSB,DEV=0", command)
        self.assertIn("--file-type", command)
        self.assertIn("raw", command)
        self.assertNotIn("hw:7", command)


class BufferAndLevelTests(unittest.TestCase):
    def test_buffer_is_bounded_and_keeps_newest_pcm(self) -> None:
        buffer = BoundedPCMBuffer(2)
        buffer.put(b"first")
        buffer.put(b"second")
        buffer.put(b"third")
        self.assertEqual(buffer.get(), b"second")
        self.assertEqual(buffer.get(), b"third")
        self.assertEqual(buffer.snapshot()["overruns"], 1)

    def test_level_meter_detects_silence_and_clipping(self) -> None:
        meter = PCMLevelMeter(silence_dbfs=-45, clipping_ratio=0.98)
        meter.observe(pcm(0, 0, 0, 0))
        silent = meter.snapshot()
        self.assertTrue(silent["silent"])
        self.assertEqual(silent["consecutive_silent_chunks"], 1)

        meter.observe(pcm(32_767, -32_768, 10_000, -10_000))
        loud = meter.snapshot()
        self.assertFalse(loud["silent"])
        self.assertTrue(loud["clipping"])
        self.assertEqual(loud["clipped_chunks"], 1)
        self.assertEqual(loud["consecutive_silent_chunks"], 0)


class FakeCapture:
    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = chunks
        self.closed = threading.Event()

    def read(self, _size: int) -> bytes:
        if self.closed.is_set():
            return b""
        if self.chunks:
            value = self.chunks.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        time.sleep(0.005)
        return pcm(1_000, -1_000)

    def close(self) -> None:
        self.closed.set()


class FakePlayback:
    def __init__(self, written: list[bytes]) -> None:
        self.written = written

    def write(self, chunk: bytes) -> None:
        self.written.append(chunk)

    def close(self) -> None:
        return


class RecoveringBackend:
    device = AudioDevice("MisoUSB", 7, 0, "USB Audio", True, True)

    def __init__(self) -> None:
        self.capture_opens = 0
        self.playback_opens = 0
        self.written: list[bytes] = []

    def available(self) -> bool:
        return True

    def devices(self) -> tuple[AudioDevice, ...]:
        return (self.device,)

    def open_capture(self, _device: AudioDevice, _format: AudioFormat) -> FakeCapture:
        self.capture_opens += 1
        if self.capture_opens == 1:
            return FakeCapture([pcm(32_767, -32_768), OSError("USB disconnected")])
        return FakeCapture([pcm(2_000, -2_000)])

    def open_playback(self, _device: AudioDevice, _format: AudioFormat) -> FakePlayback:
        self.playback_opens += 1
        return FakePlayback(self.written)


class AudioManagerTests(unittest.TestCase):
    def test_streams_reports_levels_and_recovers_capture(self) -> None:
        backend = RecoveringBackend()
        manager = AudioManager(
            enabled=True,
            capture_card="MisoUSB",
            playback_card="MisoUSB",
            device_index=0,
            sample_rate=16_000,
            channels=1,
            chunk_milliseconds=20,
            buffer_milliseconds=40,
            reconnect_seconds=0.01,
            silence_dbfs=-50,
            clipping_ratio=0.98,
            backend=backend,
        )
        manager.start()
        manager.play(pcm(4_000, -4_000))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = manager.status()
            if (
                status["capture"]["reconnections"] >= 1
                and backend.written
            ):
                break
            time.sleep(0.01)
        manager.stop()

        status = manager.status()
        self.assertGreaterEqual(backend.capture_opens, 2)
        self.assertEqual(status["capture"]["device_losses"], 1)
        self.assertGreaterEqual(status["capture"]["reconnections"], 1)
        self.assertGreaterEqual(status["capture"]["levels"]["chunks"], 2)
        self.assertGreaterEqual(status["capture"]["levels"]["clipped_chunks"], 1)
        self.assertEqual(backend.written, [pcm(4_000, -4_000)])
        self.assertGreaterEqual(status["playback"]["underruns"], 1)
        self.assertEqual(status["capture"]["available_device"]["card_id"], "MisoUSB")

    def test_disabled_manager_rejects_playback(self) -> None:
        manager = AudioManager(
            enabled=False,
            capture_card=None,
            playback_card=None,
            device_index=0,
            sample_rate=16_000,
            channels=1,
            chunk_milliseconds=20,
            buffer_milliseconds=1_000,
            reconnect_seconds=1,
            silence_dbfs=-50,
            clipping_ratio=0.98,
            backend=RecoveringBackend(),
        )
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            manager.play(pcm(1, -1))
        self.assertFalse(manager.status()["enabled"])


if __name__ == "__main__":
    unittest.main()
