"""ALSA device discovery, bounded PCM streams, and audio diagnostics."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import threading
import time
from array import array
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_CARD_PATTERN = re.compile(r"^\s*(\d+)\s+\[([^]]+)]\s*:\s*(.*)$")
_PCM_PATTERN = re.compile(r"^(\d+)-(\d+):\s*(.*?)\s*:\s*(.*)$")


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """Raw PCM format shared by the capture and playback streams."""

    sample_rate: int = 16_000
    channels: int = 1
    sample_width: int = 2
    chunk_milliseconds: int = 20

    @property
    def chunk_frames(self) -> int:
        return max(1, self.sample_rate * self.chunk_milliseconds // 1000)

    @property
    def chunk_bytes(self) -> int:
        return self.chunk_frames * self.channels * self.sample_width


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """An ALSA PCM endpoint addressed by stable card ID, not card index."""

    card_id: str
    card_index: int
    device_index: int
    name: str
    capture: bool
    playback: bool

    @property
    def pcm_name(self) -> str:
        return f"plughw:CARD={self.card_id},DEV={self.device_index}"

    def public_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "device_index": self.device_index,
            "name": self.name,
            "capture": self.capture,
            "playback": self.playback,
            "pcm_name": self.pcm_name,
        }


@dataclass(frozen=True, slots=True)
class AudioSelector:
    card_id: str | None = None
    device_index: int = 0

    @property
    def configured(self) -> str:
        return self.card_id or "auto"


def discover_alsa_devices(proc_root: Path = Path("/proc/asound")) -> tuple[AudioDevice, ...]:
    """Read ALSA's kernel inventory without depending on localized CLI output."""

    try:
        cards_text = (proc_root / "cards").read_text(encoding="utf-8")
        pcm_text = (proc_root / "pcm").read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return ()

    card_ids: dict[int, str] = {}
    card_names: dict[int, str] = {}
    for line in cards_text.splitlines():
        match = _CARD_PATTERN.match(line)
        if not match:
            continue
        card_index = int(match.group(1))
        card_ids[card_index] = match.group(2).strip()
        card_names[card_index] = match.group(3).strip()

    devices: list[AudioDevice] = []
    for line in pcm_text.splitlines():
        match = _PCM_PATTERN.match(line.strip())
        if not match:
            continue
        card_index = int(match.group(1))
        device_index = int(match.group(2))
        card_id = card_ids.get(card_index)
        if not card_id:
            continue
        capabilities = match.group(4).casefold()
        devices.append(
            AudioDevice(
                card_id=card_id,
                card_index=card_index,
                device_index=device_index,
                name=match.group(3).strip() or card_names.get(card_index, card_id),
                capture="capture" in capabilities,
                playback="playback" in capabilities,
            )
        )
    return tuple(sorted(devices, key=lambda item: (item.card_index, item.device_index)))


def select_audio_device(
    devices: tuple[AudioDevice, ...],
    selector: AudioSelector,
    *,
    direction: str,
) -> AudioDevice | None:
    """Resolve a stable selector against the current, potentially renumbered cards."""

    if direction not in {"capture", "playback"}:
        raise ValueError("audio direction must be capture or playback")
    compatible = [device for device in devices if getattr(device, direction)]
    if selector.card_id is not None:
        compatible = [device for device in compatible if device.card_id == selector.card_id]
    exact = [device for device in compatible if device.device_index == selector.device_index]
    if exact:
        return exact[0]
    if selector.card_id is None and compatible:
        return compatible[0]
    return None


class BoundedPCMBuffer:
    """A bounded, thread-safe PCM queue that drops the oldest chunk on overflow."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("audio buffer capacity must be positive")
        self.capacity = capacity
        self._chunks: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._overruns = 0

    def put(self, chunk: bytes) -> None:
        if not chunk:
            return
        value = bytes(chunk)
        with self._condition:
            if len(self._chunks) >= self.capacity:
                self._chunks.popleft()
                self._overruns += 1
            self._chunks.append(value)
            self._condition.notify()

    def put_wait(
        self,
        chunk: bytes,
        timeout: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Add without dropping audio, applying bounded producer backpressure."""

        if not chunk:
            return True
        value = bytes(chunk)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while len(self._chunks) >= self.capacity:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("audio playback was cancelled")
                remaining = (
                    None if deadline is None else deadline - time.monotonic()
                )
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(
                    0.01 if remaining is None else min(0.01, remaining)
                )
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("audio playback was cancelled")
            self._chunks.append(value)
            self._condition.notify_all()
            return True

    def get(self, timeout: float | None = None) -> bytes | None:
        with self._condition:
            if not self._chunks:
                self._condition.wait(timeout)
            value = self._chunks.popleft() if self._chunks else None
            if value is not None:
                self._condition.notify_all()
            return value

    def clear(self) -> int:
        with self._condition:
            removed = len(self._chunks)
            self._chunks.clear()
            self._condition.notify_all()
            return removed

    def wake(self) -> None:
        with self._condition:
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._condition:
            return {
                "queued_chunks": len(self._chunks),
                "capacity_chunks": self.capacity,
                "overruns": self._overruns,
            }


class PCMLevelMeter:
    """Track S16_LE peak/RMS levels, clipping, and consecutive silence."""

    def __init__(self, silence_dbfs: float, clipping_ratio: float) -> None:
        self.silence_dbfs = silence_dbfs
        self.clipping_ratio = clipping_ratio
        self._lock = threading.Lock()
        self._peak_dbfs = -120.0
        self._rms_dbfs = -120.0
        self._clipping = False
        self._silent = True
        self._clipped_chunks = 0
        self._silent_chunks = 0
        self._consecutive_silent_chunks = 0
        self._chunks = 0

    @staticmethod
    def _dbfs(amplitude: float) -> float:
        if amplitude <= 0:
            return -120.0
        return max(-120.0, 20.0 * math.log10(amplitude / 32767.0))

    def observe(self, pcm: bytes) -> None:
        usable = len(pcm) - (len(pcm) % 2)
        if not usable:
            return
        samples = array("h")
        samples.frombytes(pcm[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        peak = max(abs(value) for value in samples)
        rms = math.sqrt(sum(value * value for value in samples) / len(samples))
        peak_dbfs = self._dbfs(peak)
        rms_dbfs = self._dbfs(rms)
        clipping = peak >= round(32767 * self.clipping_ratio)
        silent = rms_dbfs <= self.silence_dbfs
        with self._lock:
            self._peak_dbfs = peak_dbfs
            self._rms_dbfs = rms_dbfs
            self._clipping = clipping
            self._silent = silent
            self._chunks += 1
            self._clipped_chunks += int(clipping)
            self._silent_chunks += int(silent)
            self._consecutive_silent_chunks = (
                self._consecutive_silent_chunks + 1 if silent else 0
            )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "peak_dbfs": round(self._peak_dbfs, 2),
                "rms_dbfs": round(self._rms_dbfs, 2),
                "clipping": self._clipping,
                "silent": self._silent,
                "chunks": self._chunks,
                "clipped_chunks": self._clipped_chunks,
                "silent_chunks": self._silent_chunks,
                "consecutive_silent_chunks": self._consecutive_silent_chunks,
            }


class CaptureHandle(Protocol):
    def read(self, size: int) -> bytes: ...

    def close(self) -> None: ...


class PlaybackHandle(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def close(self) -> None: ...


class AudioBackend(Protocol):
    def devices(self) -> tuple[AudioDevice, ...]: ...

    def open_capture(
        self, device: AudioDevice, audio_format: AudioFormat
    ) -> CaptureHandle: ...

    def open_playback(
        self, device: AudioDevice, audio_format: AudioFormat
    ) -> PlaybackHandle: ...

    def available(self) -> bool: ...


class _SubprocessCapture:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def read(self, size: int) -> bytes:
        if self.process.stdout is None:
            return b""
        return self.process.stdout.read(size)

    def close(self) -> None:
        _close_process(self.process)


class _SubprocessPlayback:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def write(self, chunk: bytes) -> None:
        if self.process.stdin is None:
            raise BrokenPipeError("ALSA playback pipe is closed")
        self.process.stdin.write(chunk)

    def close(self) -> None:
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except (OSError, ValueError):
                pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=600)
            except subprocess.TimeoutExpired:
                self.cancel()

    def cancel(self) -> None:
        _close_process(self.process)


def _close_process(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


class ALSABackend:
    """Raw PCM transport implemented with the standard alsa-utils commands."""

    def __init__(self, proc_root: Path = Path("/proc/asound")) -> None:
        self.proc_root = proc_root

    def available(self) -> bool:
        return shutil.which("arecord") is not None and shutil.which("aplay") is not None

    def devices(self) -> tuple[AudioDevice, ...]:
        return discover_alsa_devices(self.proc_root)

    @staticmethod
    def _command(name: str, device: AudioDevice, audio_format: AudioFormat) -> list[str]:
        return [
            name,
            "--quiet",
            "--device",
            device.pcm_name,
            "--format",
            "S16_LE",
            "--rate",
            str(audio_format.sample_rate),
            "--channels",
            str(audio_format.channels),
            "--file-type",
            "raw",
        ]

    def open_capture(
        self, device: AudioDevice, audio_format: AudioFormat
    ) -> CaptureHandle:
        command = self._command("arecord", device, audio_format)
        command.append("--fatal-errors")
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return _SubprocessCapture(process)

    def open_playback(
        self, device: AudioDevice, audio_format: AudioFormat
    ) -> PlaybackHandle:
        process = subprocess.Popen(
            self._command("aplay", device, audio_format),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        return _SubprocessPlayback(process)


class _EndpointState:
    def __init__(self, initial: str) -> None:
        self._lock = threading.Lock()
        self._state = initial
        self._device: AudioDevice | None = None
        self._last_error: str | None = None
        self._device_losses = 0
        self._reconnections = 0
        self._connected_once = False
        self._underruns = 0

    def set_state(self, state: str, error: Exception | str | None = None) -> None:
        with self._lock:
            self._state = state
            self._last_error = None if error is None else str(error)[:200]

    def connected(self, device: AudioDevice) -> None:
        with self._lock:
            if self._connected_once:
                self._reconnections += 1
            self._connected_once = True
            self._device = device
            self._state = "streaming"
            self._last_error = None

    def disconnected(self, error: Exception | str) -> None:
        with self._lock:
            self._device_losses += 1
            self._device = None
            self._state = "reconnecting"
            self._last_error = str(error)[:200]

    def idle(self) -> None:
        with self._lock:
            self._device = None
            self._state = "idle"
            self._last_error = None

    def underrun(self) -> None:
        with self._lock:
            self._underruns += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state,
                "device": None if self._device is None else self._device.public_dict(),
                "last_error": self._last_error,
                "device_losses": self._device_losses,
                "reconnections": self._reconnections,
                "underruns": self._underruns,
            }


class _AudioEndpoint:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self._handle_lock = threading.Lock()
        self._handle: CaptureHandle | PlaybackHandle | None = None
        self._thread: threading.Thread | None = None

    def _set_handle(self, handle: CaptureHandle | PlaybackHandle | None) -> None:
        with self._handle_lock:
            self._handle = handle

    def _close_handle(self, *, cancel: bool = False) -> None:
        with self._handle_lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            cancel_method = getattr(handle, "cancel", None)
            if cancel and cancel_method is not None:
                cancel_method()
            else:
                handle.close()

    def start(self, target: object, name: str) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.stop_event.clear()
        self._thread = threading.Thread(target=target, name=name, daemon=True)
        self._thread.start()

    def stop(self, buffer: BoundedPCMBuffer) -> None:
        self.stop_event.set()
        buffer.wake()
        self._close_handle(cancel=True)
        if self._thread is not None:
            self._thread.join(timeout=2)


class _CaptureEndpoint(_AudioEndpoint):
    def __init__(
        self,
        backend: AudioBackend,
        selector: AudioSelector,
        audio_format: AudioFormat,
        publish: Callable[[bytes], None],
        meter: PCMLevelMeter,
        reconnect_seconds: float,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.selector = selector
        self.audio_format = audio_format
        self.publish = publish
        self.meter = meter
        self.reconnect_seconds = reconnect_seconds
        self.state = _EndpointState("searching")

    def start_capture(self) -> None:
        self.start(self._run, "miso-audio-capture")

    def _run(self) -> None:
        while not self.stop_event.is_set():
            handle: CaptureHandle | None = None
            try:
                device = select_audio_device(
                    self.backend.devices(), self.selector, direction="capture"
                )
                if device is None:
                    self.state.set_state(
                        "unavailable", "configured capture device not found"
                    )
                    self.stop_event.wait(self.reconnect_seconds)
                    continue
                handle = self.backend.open_capture(device, self.audio_format)
                self._set_handle(handle)
                self.state.connected(device)
                while not self.stop_event.is_set():
                    chunk = handle.read(self.audio_format.chunk_bytes)
                    if not chunk:
                        raise OSError("capture stream ended")
                    self.meter.observe(chunk)
                    self.publish(chunk)
            except (OSError, subprocess.SubprocessError) as error:
                if not self.stop_event.is_set():
                    self.state.disconnected(error)
                    self.stop_event.wait(self.reconnect_seconds)
            finally:
                if handle is not None:
                    self._close_handle()
        self.state.set_state("stopped")


class _PlaybackEndpoint(_AudioEndpoint):
    def __init__(
        self,
        backend: AudioBackend,
        selector: AudioSelector,
        audio_format: AudioFormat,
        buffer: BoundedPCMBuffer,
        meter: PCMLevelMeter,
        reconnect_seconds: float,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.selector = selector
        self.audio_format = audio_format
        self.buffer = buffer
        self.meter = meter
        self.reconnect_seconds = reconnect_seconds
        self.state = _EndpointState("idle")
        self._interrupt_lock = threading.Lock()
        self._interrupt_serial = 0

    def start_playback(self) -> None:
        self.start(self._run, "miso-audio-playback")

    def interrupt(self) -> None:
        with self._interrupt_lock:
            self._interrupt_serial += 1
        self.buffer.clear()
        self.buffer.wake()
        self._close_handle(cancel=True)

    def _current_interrupt_serial(self) -> int:
        with self._interrupt_lock:
            return self._interrupt_serial

    def _run(self) -> None:
        pending: bytes | None = None
        pending_interrupt_serial = 0
        while not self.stop_event.is_set():
            if pending is None:
                pending = self.buffer.get(timeout=0.25)
                if pending is None:
                    continue
                pending_interrupt_serial = self._current_interrupt_serial()
            if pending_interrupt_serial != self._current_interrupt_serial():
                pending = None
                self.state.idle()
                continue
            handle: PlaybackHandle | None = None
            interrupt_serial = pending_interrupt_serial
            drained = False
            try:
                device = select_audio_device(
                    self.backend.devices(), self.selector, direction="playback"
                )
                if device is None:
                    self.state.set_state(
                        "unavailable", "configured playback device not found"
                    )
                    self.stop_event.wait(self.reconnect_seconds)
                    continue
                handle = self.backend.open_playback(device, self.audio_format)
                self._set_handle(handle)
                self.state.connected(device)
                while pending is not None and not self.stop_event.is_set():
                    handle.write(pending)
                    self.meter.observe(pending)
                    pending = self.buffer.get(
                        timeout=max(0.05, self.audio_format.chunk_milliseconds / 500.0)
                    )
                    if pending is None:
                        self.state.underrun()
                self.state.set_state("draining")
                drained = True
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                if not self.stop_event.is_set():
                    if interrupt_serial != self._current_interrupt_serial():
                        pending = None
                        self.state.idle()
                    else:
                        self.state.disconnected(error)
                        self.stop_event.wait(self.reconnect_seconds)
            finally:
                if handle is not None:
                    self._close_handle()
                if drained:
                    self.state.idle()
        self.state.set_state("stopped")


class AudioManager:
    """Own capture/playback workers and provide a redacted diagnostic snapshot."""

    def __init__(
        self,
        *,
        enabled: bool,
        capture_card: str | None,
        playback_card: str | None,
        device_index: int,
        sample_rate: int,
        channels: int,
        chunk_milliseconds: int,
        buffer_milliseconds: int,
        reconnect_seconds: float,
        silence_dbfs: float,
        clipping_ratio: float,
        playback_sample_rate: int | None = None,
        backend: AudioBackend | None = None,
    ) -> None:
        self.enabled = enabled
        self.backend = backend or ALSABackend()
        self.audio_format = AudioFormat(sample_rate, channels, 2, chunk_milliseconds)
        self.playback_format = AudioFormat(
            playback_sample_rate or sample_rate,
            channels,
            2,
            chunk_milliseconds,
        )
        capacity = max(1, math.ceil(buffer_milliseconds / chunk_milliseconds))
        self.capture_buffer = BoundedPCMBuffer(capacity)
        self._capture_subscribers: set[BoundedPCMBuffer] = set()
        self._capture_subscribers_lock = threading.Lock()
        self.playback_buffer = BoundedPCMBuffer(capacity)
        self.capture_meter = PCMLevelMeter(silence_dbfs, clipping_ratio)
        self.playback_meter = PCMLevelMeter(silence_dbfs, clipping_ratio)
        self.capture_selector = AudioSelector(capture_card, device_index)
        self.playback_selector = AudioSelector(playback_card, device_index)
        self.capture = _CaptureEndpoint(
            self.backend,
            self.capture_selector,
            self.audio_format,
            self._publish_capture,
            self.capture_meter,
            reconnect_seconds,
        )
        self.playback = _PlaybackEndpoint(
            self.backend,
            self.playback_selector,
            self.playback_format,
            self.playback_buffer,
            self.playback_meter,
            reconnect_seconds,
        )

    def start(self) -> None:
        if self.enabled:
            self.capture.start_capture()
            self.playback.start_playback()

    def stop(self) -> None:
        self.capture.stop(self.capture_buffer)
        self.playback.stop(self.playback_buffer)

    def read_capture(self, timeout: float | None = None) -> bytes | None:
        return self.capture_buffer.get(timeout)

    def subscribe_capture(self, capacity: int | None = None) -> BoundedPCMBuffer:
        """Create an independent bounded tap on the live capture stream."""

        subscriber = BoundedPCMBuffer(capacity or self.capture_buffer.capacity)
        with self._capture_subscribers_lock:
            self._capture_subscribers.add(subscriber)
        return subscriber

    def unsubscribe_capture(self, subscriber: BoundedPCMBuffer) -> None:
        with self._capture_subscribers_lock:
            self._capture_subscribers.discard(subscriber)
        subscriber.wake()

    def _publish_capture(self, chunk: bytes) -> None:
        self.capture_buffer.put(chunk)
        with self._capture_subscribers_lock:
            subscribers = tuple(self._capture_subscribers)
        for subscriber in subscribers:
            subscriber.put(chunk)

    def play(self, pcm: bytes) -> None:
        if not self.enabled:
            raise RuntimeError("audio is disabled")
        if len(pcm) % (
            self.playback_format.channels * self.playback_format.sample_width
        ):
            raise ValueError("PCM payload does not contain complete frames")
        self.playback_buffer.put(pcm)

    def play_stream(
        self,
        pcm: bytes,
        timeout: float = 1.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if not self.enabled:
            raise RuntimeError("audio is disabled")
        if len(pcm) % (
            self.playback_format.channels * self.playback_format.sample_width
        ):
            raise ValueError("PCM payload does not contain complete frames")
        if not self.playback_buffer.put_wait(pcm, timeout, cancel_event):
            raise TimeoutError("audio playback buffer remained full")

    def cancel_playback(self) -> None:
        self.playback.interrupt()

    def wait_playback(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            queued = self.playback_buffer.snapshot()["queued_chunks"]
            state = self.playback.state.snapshot()["state"]
            if queued == 0 and state in {"idle", "stopped"}:
                return True
            time.sleep(0.01)
        return False

    def status(self) -> dict[str, object]:
        devices = self.backend.devices() if self.enabled else ()
        capture_device = select_audio_device(
            devices, self.capture_selector, direction="capture"
        )
        playback_device = select_audio_device(
            devices, self.playback_selector, direction="playback"
        )
        return {
            "enabled": self.enabled,
            "backend_available": self.backend.available() if self.enabled else False,
            "format": {
                "encoding": "S16_LE",
                "sample_rate": self.audio_format.sample_rate,
                "channels": self.audio_format.channels,
                "chunk_milliseconds": self.audio_format.chunk_milliseconds,
            },
            "capture": {
                "configured_card": self.capture_selector.configured,
                "available_device": (
                    None if capture_device is None else capture_device.public_dict()
                ),
                **self.capture.state.snapshot(),
                "buffer": self.capture_buffer.snapshot(),
                "levels": self.capture_meter.snapshot(),
            },
            "playback": {
                "format": {
                    "encoding": "S16_LE",
                    "sample_rate": self.playback_format.sample_rate,
                    "channels": self.playback_format.channels,
                    "chunk_milliseconds": self.playback_format.chunk_milliseconds,
                },
                "configured_card": self.playback_selector.configured,
                "available_device": (
                    None if playback_device is None else playback_device.public_dict()
                ),
                **self.playback.state.snapshot(),
                "buffer": self.playback_buffer.snapshot(),
                "levels": self.playback_meter.snapshot(),
            },
        }
