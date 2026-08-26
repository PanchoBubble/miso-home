import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from miso.display import DisplayWakeNotifier
from miso.wake import WakeEvent


class DisplayWakeNotifierTests(unittest.TestCase):
    def test_disabled_notifier_is_a_noop(self) -> None:
        notifier = DisplayWakeNotifier(None)
        self.assertFalse(notifier.enabled)
        notifier.notify(WakeEvent("Miso", 0.99, 1.0))

    def test_activation_creates_and_touches_marker_without_content(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wake"
            notifier = DisplayWakeNotifier(path)
            self.assertTrue(notifier.enabled)
            notifier.notify(WakeEvent("Miso", 0.99, 1.0))
            self.assertEqual(path.read_bytes(), b"")
            os.utime(path, ns=(1, 1))
            notifier.notify(WakeEvent("Miso", 0.99, 2.0))
            self.assertGreater(path.stat().st_mtime_ns, 1)

    def test_each_activation_advances_marker_when_clock_does_not(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "wake"
            path.touch()
            notifier = DisplayWakeNotifier(path)
            initial = path.stat().st_mtime_ns
            with patch("miso.display.time.time_ns", return_value=initial):
                notifier.notify(WakeEvent("Miso", 0.99, 1.0))
                first = path.stat().st_mtime_ns
                notifier.notify(WakeEvent("Miso", 0.99, 2.0))
                second = path.stat().st_mtime_ns
            self.assertEqual(first, initial + 1)
            self.assertEqual(second, first + 1)

    def test_missing_runtime_directory_does_not_break_voice_path(self) -> None:
        with TemporaryDirectory() as directory:
            notifier = DisplayWakeNotifier(Path(directory) / "missing" / "wake")
            with self.assertLogs("miso.display", level="WARNING"):
                notifier.notify(WakeEvent("Miso", 0.99, 1.0))


if __name__ == "__main__":
    unittest.main()
