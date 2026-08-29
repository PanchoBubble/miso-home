"""Least-privilege bridge from Miso's system service to desktop Pulse audio."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import threading
from typing import BinaryIO


def pulse_environment() -> dict[str, str]:
    environment = dict(os.environ)
    runtime_dir = environment.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    environment.setdefault("PULSE_SERVER", f"unix:{runtime_dir}/pulse/native")
    return environment


def discover_sinks() -> list[dict[str, str]]:
    result = subprocess.run(
        ["pactl", "--format=json", "list", "sinks"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=pulse_environment(),
        timeout=3,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(detail or "Pulse server is unavailable")
    try:
        values = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError("pactl returned invalid sink data") from error
    if not isinstance(values, list):
        raise OSError("pactl returned invalid sink data")
    sinks: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            continue
        properties = value.get("properties", {})
        description = value["name"]
        if isinstance(properties, dict):
            candidate = properties.get("device.description")
            if isinstance(candidate, str):
                description = candidate
        sinks.append({"name": value["name"], "description": description})
    return sinks


def playback_command(request: dict[str, object]) -> list[str]:
    sink = request.get("sink")
    sample_rate = request.get("sample_rate")
    channels = request.get("channels")
    sample_width = request.get("sample_width")
    if (
        not isinstance(sink, str)
        or not sink
        or len(sink) > 255
        or not all(character.isalnum() or character in "_.:-" for character in sink)
    ):
        raise ValueError("invalid Pulse sink")
    if not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 192_000:
        raise ValueError("invalid sample rate")
    if not isinstance(channels, int) or not 1 <= channels <= 8:
        raise ValueError("invalid channel count")
    if sample_width != 2:
        raise ValueError("only S16_LE playback is supported")
    return [
        "paplay",
        "--raw",
        f"--device={sink}",
        f"--rate={sample_rate}",
        f"--channels={channels}",
        "--format=s16le",
        "--client-name=Miso",
        "--stream-name=Miso speech",
    ]


def _send(connection: socket.socket, value: dict[str, object]) -> None:
    connection.sendall(json.dumps(value, separators=(",", ":")).encode() + b"\n")


def _read_request(stream: BinaryIO) -> dict[str, object]:
    line = stream.readline(65_537)
    if not line or len(line) > 65_536 or not line.endswith(b"\n"):
        raise ValueError("invalid proxy request")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("invalid proxy request")
    return value


def _receive_exact(connection: socket.socket, size: int) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise ConnectionError("playback client disconnected")
        payload.extend(chunk)
    return bytes(payload)


class PulseProxyHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = _read_request(self.rfile)
            action = request.get("action")
            if action == "devices":
                _send(self.connection, {"ok": True, "sinks": discover_sinks()})
                return
            if action != "play":
                raise ValueError("unsupported proxy action")
            command = playback_command(request)
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=pulse_environment(),
                bufsize=0,
            )
            _send(self.connection, {"ok": True})
            cancelled = False
            try:
                assert process.stdin is not None
                while True:
                    size = struct.unpack("!I", _receive_exact(self.connection, 4))[0]
                    if size == 0:
                        break
                    if size == 0xFFFFFFFF:
                        cancelled = True
                        process.terminate()
                        try:
                            process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=1)
                        return
                    if size > 16 * 1024 * 1024:
                        raise ValueError("playback frame is too large")
                    chunk = _receive_exact(self.connection, size)
                    process.stdin.write(chunk)
                process.stdin.close()
                return_code = process.wait(timeout=600)
            except (ConnectionError, BrokenPipeError, OSError):
                cancelled = True
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
                return_code = process.returncode
            if cancelled:
                return
            if return_code != 0:
                assert process.stderr is not None
                detail = process.stderr.read(4096).decode("utf-8", errors="replace").strip()
                _send(self.connection, {"ok": False, "error": detail or "Pulse playback failed"})
            else:
                _send(self.connection, {"ok": True})
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
            try:
                _send(self.connection, {"ok": False, "error": str(error)[:200]})
            except OSError:
                pass


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(socket_path: Path) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = ThreadingUnixServer(str(socket_path), PulseProxyHandler)
    os.chmod(socket_path, 0o660)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        socket_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, default=Path("/run/miso-audio/playback.sock"))
    options = parser.parse_args()
    serve(options.socket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
