#!/usr/bin/env python3
"""Blank a Wayland output after inactivity and reset it on Miso wake events."""

from __future__ import annotations

import logging
import os
import re
import shlex
import signal
import subprocess
import threading
from pathlib import Path

LOGGER = logging.getLogger("miso.display-idle")
OUTPUT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class DisplayIdleManager:
    def __init__(
        self,
        *,
        output: str,
        idle_seconds: int,
        wake_path: Path,
    ) -> None:
        if not OUTPUT_PATTERN.fullmatch(output):
            raise ValueError("display output name is invalid")
        if not 10 <= idle_seconds <= 86_400:
            raise ValueError("display idle timeout must be between 10 and 86400 seconds")
        if not wake_path.is_absolute():
            raise ValueError("display wake path must be absolute")
        self.output = output
        self.idle_seconds = idle_seconds
        self.wake_path = wake_path
        self.stop_event = threading.Event()
        self.process: subprocess.Popen[bytes] | None = None
        self.last_wake_ns = self._wake_mtime()

    def _wake_mtime(self) -> int:
        try:
            return self.wake_path.stat().st_mtime_ns
        except FileNotFoundError:
            return 0

    def _power_command(self, state: str) -> list[str]:
        return ["/usr/bin/wlopm", f"--{state}", self.output]

    def _start_idle_monitor(self) -> None:
        output = shlex.quote(self.output)
        self.process = subprocess.Popen(
            [
                "/usr/bin/swayidle",
                "-w",
                "timeout",
                str(self.idle_seconds),
                f"/usr/bin/wlopm --off {output}",
                "resume",
                f"/usr/bin/wlopm --on {output}",
            ]
        )
        LOGGER.info(
            "display idle monitor started for %s with %ss timeout",
            self.output,
            self.idle_seconds,
        )

    def _stop_idle_monitor(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def wake(self) -> None:
        # Restarting swayidle resets its inactivity countdown. SIGTERM also runs
        # its pending resume command; the explicit power-on call is a safe backup.
        self._stop_idle_monitor()
        subprocess.run(
            self._power_command("on"),
            check=False,
            timeout=3,
        )
        LOGGER.info("display wake requested")
        self._start_idle_monitor()

    def run(self) -> None:
        while not self.stop_event.is_set():
            if self.process is None or self.process.poll() is not None:
                if self.process is not None:
                    LOGGER.warning("display idle monitor exited; retrying")
                self._start_idle_monitor()
            wake_ns = self._wake_mtime()
            if wake_ns > self.last_wake_ns:
                self.last_wake_ns = wake_ns
                self.wake()
            self.stop_event.wait(0.2)
        self._stop_idle_monitor()

    def stop(self, _signal: int, _frame: object) -> None:
        self.stop_event.set()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("MISO_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    manager = DisplayIdleManager(
        output=os.environ.get("MISO_DISPLAY_OUTPUT", "DSI-2"),
        idle_seconds=int(os.environ.get("MISO_DISPLAY_IDLE_SECONDS", "300")),
        wake_path=Path(
            os.environ.get("MISO_DISPLAY_WAKE_PATH", "/run/miso-display/wake")
        ),
    )
    signal.signal(signal.SIGTERM, manager.stop)
    signal.signal(signal.SIGINT, manager.stop)
    manager.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
