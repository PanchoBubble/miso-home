"""Thread-safe audit sinks for tool activity."""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Protocol


class AuditSink(Protocol):
    def record(self, event: Mapping[str, object]) -> None: ...


def audit_event(event: str, **fields: object) -> dict[str, object]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "event": event,
        **fields,
    }


def redact(value: object, fields: frozenset[str]) -> object:
    """Return a JSON-safe copy with named fields recursively redacted."""
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key) in fields else redact(item, fields)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, fields) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def record(self, event: Mapping[str, object]) -> None:
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self._events.append(json.loads(encoded))

    def events(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)


class JsonlAuditLog:
    """Append-only JSONL audit log created with private file permissions."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("audit log path must be absolute")
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: Mapping[str, object]) -> None:
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
