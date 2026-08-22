"""Expiring, scoped developer command mode; disabled by default."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miso.tools.audit import AuditSink, InMemoryAuditLog, audit_event
from miso.tools.base import (
    ToolCancelled,
    ToolContext,
    ToolDeadlineExceeded,
    ToolDefinition,
    ToolRejected,
)


class DeveloperShellController:
    """Dashboard-facing lifecycle control for the dangerous developer tool."""

    def __init__(
        self,
        root: Path,
        allowed_executables: Sequence[str],
        *,
        audit_sink: AuditSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        maximum_duration_seconds: int = 900,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("developer shell root must be absolute")
        if maximum_duration_seconds < 1:
            raise ValueError("maximum developer mode duration must be positive")
        commands = frozenset(allowed_executables)
        if not commands or any(
            not command or Path(command).name != command for command in commands
        ):
            raise ValueError("allowed executables must be non-empty command names")
        self.root = root.resolve()
        self.allowed_executables = commands
        self.audit_sink = audit_sink or InMemoryAuditLog()
        self._monotonic = monotonic
        self._now = now
        self.maximum_duration_seconds = maximum_duration_seconds
        self._enabled_until = 0.0
        self._expires_at: datetime | None = None
        self._approved_by: str | None = None
        self._lock = threading.Lock()

    def enable(self, duration_seconds: int, *, approved_by: str) -> dict[str, object]:
        if not approved_by.strip():
            raise ValueError("developer mode approver must not be empty")
        if not 1 <= duration_seconds <= self.maximum_duration_seconds:
            raise ValueError(
                "developer mode duration must be between 1 and "
                f"{self.maximum_duration_seconds} seconds"
            )
        now = self._now()
        with self._lock:
            self._enabled_until = self._monotonic() + duration_seconds
            self._expires_at = now + timedelta(seconds=duration_seconds)
            self._approved_by = approved_by
        self.audit_sink.record(
            audit_event(
                "developer_mode_enabled",
                approved_by=approved_by,
                duration_seconds=duration_seconds,
                scope=str(self.root),
                allowed_executables=sorted(self.allowed_executables),
            )
        )
        return self.status()

    def disable(self, *, actor: str) -> dict[str, object]:
        with self._lock:
            was_enabled = self._enabled_unlocked()
            self._enabled_until = 0.0
            self._expires_at = None
            self._approved_by = None
        self.audit_sink.record(
            audit_event(
                "developer_mode_disabled",
                actor=actor,
                was_enabled=was_enabled,
                scope=str(self.root),
            )
        )
        return self.status()

    def status(self) -> dict[str, object]:
        with self._lock:
            enabled = self._enabled_unlocked()
            if not enabled:
                self._enabled_until = 0.0
                self._expires_at = None
                self._approved_by = None
            return {
                "enabled": enabled,
                "expires_at": self._expires_at.isoformat() if self._expires_at else None,
                "scope": str(self.root),
                "allowed_executables": sorted(self.allowed_executables),
                "approved_by": self._approved_by,
            }

    def _enabled_unlocked(self) -> bool:
        return self._enabled_until > self._monotonic()

    def tool_definition(self, *, timeout_seconds: float = 15.0) -> ToolDefinition:
        return ToolDefinition(
            name="developer_command",
            description="Run one allowlisted command inside the temporary developer scope",
            input_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "minItems": 1,
                        "maxItems": 64,
                    },
                    "cwd": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=self._run,
            timeout_seconds=timeout_seconds,
        )

    def _run(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> Mapping[str, object]:
        status = self.status()
        if not status["enabled"]:
            raise ToolRejected("developer mode is disabled or expired")
        command = arguments["command"]
        assert isinstance(command, (list, tuple))
        executable = str(command[0])
        if Path(executable).name != executable or executable not in self.allowed_executables:
            raise ToolRejected(f"executable is not allowlisted: {executable}")
        relative_cwd = str(arguments.get("cwd", "."))
        working_directory = (self.root / relative_cwd).resolve()
        if not working_directory.is_relative_to(self.root):
            raise ToolRejected("working directory is outside developer scope")
        if not working_directory.is_dir():
            raise ToolRejected("working directory does not exist")
        context.raise_if_cancelled()
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        }
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=working_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
        try:
            while True:
                context.raise_if_cancelled()
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.05, context.remaining_seconds())
                    )
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (ToolCancelled, ToolDeadlineExceeded):
            self._terminate(process)
            raise
        except BaseException:
            self._terminate(process)
            raise
        return {
            "exit_code": process.returncode,
            "stdout": stdout[-65536:],
            "stderr": stderr[-65536:],
            "output_truncated": len(stdout) > 65536 or len(stderr) > 65536,
        }

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=0.5)
