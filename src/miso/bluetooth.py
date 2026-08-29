"""Keep the configured Bluetooth speaker connected so voice output cannot go mute.

Pulse playback deliberately refuses to fall back to another sink, so a speaker
that has drifted off leaves Miso completely silent: the acknowledgement cue
fails, the turn errors, and the conversation never reaches its listening state.
BlueZ accepts a reconnection from a trusted device but does not initiate one, so
a bounded periodic attempt is what closes the gap.

The address is derived from the configured sink name rather than configured
again, so the speaker identity has exactly one source of truth.
"""

from __future__ import annotations

import argparse
from os import environ
import re
import subprocess
import sys
from typing import Mapping


BLUEZ_SINK_PATTERN = re.compile(
    r"^bluez_output\.([0-9A-Fa-f]{2}(?:_[0-9A-Fa-f]{2}){5})\.", re.ASCII
)

CONNECTED_PATTERN = re.compile(r"^\s*Connected:\s*yes\s*$", re.IGNORECASE | re.MULTILINE)


def sink_address(card: str | None) -> str | None:
    """Derive a Bluetooth address from a PipeWire bluez sink name."""
    if not card:
        return None
    match = BLUEZ_SINK_PATTERN.match(card)
    if match is None:
        return None
    return match.group(1).replace("_", ":").upper()


def configured_address(source: Mapping[str, str] | None = None) -> str | None:
    """Resolve the speaker to maintain, or None when this host manages none."""
    values = environ if source is None else source
    backend = values.get("MISO_AUDIO_PLAYBACK_BACKEND", "alsa").strip().casefold()
    if backend != "pulse":
        return None
    return sink_address(values.get("MISO_AUDIO_PLAYBACK_CARD", "").strip())


def _bluetoothctl(arguments: list[str], timeout: float) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["bluetoothctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def is_connected(address: str, timeout: float = 5.0) -> bool:
    code, output = _bluetoothctl(["info", address], timeout)
    return code == 0 and bool(CONNECTED_PATTERN.search(output))


def connect(address: str, timeout: float = 25.0) -> bool:
    _bluetoothctl(["--timeout", "20", "connect", address], timeout)
    # bluetoothctl's exit status does not reliably reflect the link state, so the
    # result is confirmed by re-reading the device rather than trusting it.
    return is_connected(address)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address",
        help="override the speaker address instead of deriving it from the sink",
    )
    options = parser.parse_args(argv)

    address = options.address or configured_address()
    if address is None:
        print("no Bluetooth speaker is configured; nothing to maintain")
        return 0
    if is_connected(address):
        return 0
    print(f"speaker {address} is disconnected; attempting reconnect")
    if connect(address):
        print(f"speaker {address} reconnected")
        return 0
    # A powered-off speaker is the ordinary case, not a fault: exit cleanly so
    # the timer keeps retrying without flooding the journal with failures.
    print(f"speaker {address} is unavailable; will retry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
