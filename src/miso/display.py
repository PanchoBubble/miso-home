"""Best-effort bridge from validated wake events to a local display session."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from miso.wake import WakeEvent

LOGGER = logging.getLogger("miso.display")


class DisplayWakeNotifier:
    """Touch a non-sensitive marker consumed by the display idle service."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def notify(self, _event: WakeEvent) -> None:
        if self.path is None:
            return
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
                0o640,
            )
            try:
                metadata = os.fstat(descriptor)
                # A normal touch can repeat or move backwards when the system
                # clock is corrected. Keep this marker strictly increasing so
                # every activation remains observable by the idle service.
                wake_ns = max(time.time_ns(), metadata.st_mtime_ns + 1)
                os.utime(
                    descriptor,
                    ns=(metadata.st_atime_ns, wake_ns),
                )
            finally:
                os.close(descriptor)
        except OSError as error:
            # Display integration must never interrupt the voice path.
            LOGGER.warning("could not wake local display: %s", error)
