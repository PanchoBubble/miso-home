"""Best-effort bridge from validated wake events to a local display session."""

from __future__ import annotations

import logging
import os
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
                os.utime(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            # Display integration must never interrupt the voice path.
            LOGGER.warning("could not wake local display: %s", error)
