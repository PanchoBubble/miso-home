from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).parents[2] / "ops" / "miso-display-idle.py"
SPEC = importlib.util.spec_from_file_location("miso_display_idle", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
miso_display_idle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = miso_display_idle
SPEC.loader.exec_module(miso_display_idle)


class DisplayIdleManagerTests(unittest.TestCase):
    def test_validates_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "output name"):
            miso_display_idle.DisplayIdleManager(
                output="DSI-2; poweroff",
                idle_seconds=300,
                wake_path=Path("/run/miso-display/wake"),
            )
        with self.assertRaisesRegex(ValueError, "between 10"):
            miso_display_idle.DisplayIdleManager(
                output="DSI-2",
                idle_seconds=5,
                wake_path=Path("/run/miso-display/wake"),
            )

    def test_wake_powers_on_and_restarts_idle_countdown(self) -> None:
        manager = miso_display_idle.DisplayIdleManager(
            output="DSI-2",
            idle_seconds=300,
            wake_path=Path("/run/miso-display/wake"),
        )
        with (
            patch.object(manager, "_stop_idle_monitor") as stop,
            patch.object(manager, "_start_idle_monitor") as start,
            patch.object(miso_display_idle.subprocess, "run") as run,
        ):
            manager.wake()
        stop.assert_called_once_with()
        run.assert_called_once_with(
            ["/usr/bin/wlopm", "--on", "DSI-2"],
            check=False,
            timeout=3,
        )
        start.assert_called_once_with()

    def test_each_changed_wake_marker_is_observed(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "wake"
            marker.touch()
            manager = miso_display_idle.DisplayIdleManager(
                output="DSI-2",
                idle_seconds=300,
                wake_path=marker,
            )
            initial = manager.last_wake_ns
            os.utime(marker, ns=(initial + 1, initial + 1))
            self.assertTrue(manager._consume_wake_marker())
            self.assertFalse(manager._consume_wake_marker())
            os.utime(marker, ns=(initial + 2, initial + 2))
            self.assertTrue(manager._consume_wake_marker())

    def test_changed_wake_marker_survives_clock_rollback(self) -> None:
        with TemporaryDirectory() as directory:
            marker = Path(directory) / "wake"
            marker.touch()
            manager = miso_display_idle.DisplayIdleManager(
                output="DSI-2",
                idle_seconds=300,
                wake_path=marker,
            )
            initial = manager.last_wake_ns
            os.utime(marker, ns=(initial - 1, initial - 1))
            self.assertTrue(manager._consume_wake_marker())


if __name__ == "__main__":
    unittest.main()
