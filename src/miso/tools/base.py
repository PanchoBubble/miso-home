"""Validated, audited execution boundary for locally allowlisted tools."""

from __future__ import annotations

import json
import math
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from miso.tools.audit import AuditSink, InMemoryAuditLog, audit_event, redact
from miso.tools.schema import SchemaError, validate_instance, validate_tool_schema


class ToolStatus(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Deadline and cooperative cancellation state passed to every handler."""

    invocation_id: str
    deadline: float
    cancel_event: threading.Event
    caller_cancel_event: threading.Event | None = None

    def cancelled(self) -> bool:
        return self.cancel_event.is_set() or (
            self.caller_cancel_event is not None
            and self.caller_cancel_event.is_set()
        )

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise ToolCancelled("tool invocation was cancelled")
        if self.remaining_seconds() <= 0:
            raise ToolDeadlineExceeded("tool invocation deadline exceeded")


class ToolCancelled(RuntimeError):
    """Raised by a cooperative tool after cancellation."""


class ToolDeadlineExceeded(RuntimeError):
    """Raised by a cooperative tool after its deadline."""


class ToolRejected(RuntimeError):
    """Raised when current policy does not permit a valid tool request."""


ToolHandler = Callable[[Mapping[str, object], ToolContext], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, object]
    handler: ToolHandler
    timeout_seconds: float = 10.0
    redact_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class ToolResult:
    invocation_id: str
    tool: str
    status: ToolStatus
    output: Mapping[str, object] | None
    error: str | None
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.SUCCESS

    def as_dict(self) -> dict[str, object]:
        return {
            "invocation_id": self.invocation_id,
            "tool": self.tool,
            "status": self.status.value,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


class ToolRegistry:
    """Allowlist which validates and audits every request before execution."""

    def __init__(self, audit_sink: AuditSink | None = None) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.audit_sink = audit_sink or InMemoryAuditLog()

    def register(self, definition: ToolDefinition) -> None:
        if not definition.name or not definition.name.replace("_", "").isalnum():
            raise ValueError("tool name must contain only letters, numbers, and underscores")
        if definition.name in self._tools:
            raise ValueError(f"tool is already registered: {definition.name}")
        if not definition.description.strip():
            raise ValueError("tool description must not be empty")
        if not math.isfinite(definition.timeout_seconds) or not (
            0 < definition.timeout_seconds <= 600
        ):
            raise ValueError("tool timeout must be between 0 and 600 seconds")
        if any(
            not isinstance(field_name, str) or not field_name
            for field_name in definition.redact_fields
        ):
            raise ValueError("tool redaction fields must be non-empty strings")
        validate_tool_schema(definition.input_schema)
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"tool is not allowlisted: {name}") from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def schemas(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": dict(definition.input_schema),
            }
            for definition in sorted(self._tools.values(), key=lambda item: item.name)
        )

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        cancel_event: threading.Event | None = None,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        invocation_id = str(uuid.uuid4())
        started = time.monotonic()
        definition = self._tools.get(name)
        safe_arguments = redact(
            arguments,
            definition.redact_fields if definition is not None else frozenset(),
        )
        self.audit_sink.record(
            audit_event(
                "tool_invocation_started",
                invocation_id=invocation_id,
                tool=name,
                arguments=safe_arguments,
            )
        )

        if definition is None:
            return self._finish(
                invocation_id,
                name,
                ToolStatus.REJECTED,
                started,
                error="tool is not allowlisted",
            )
        try:
            validate_instance(definition.input_schema, arguments)
        except SchemaError as error:
            return self._finish(
                invocation_id,
                name,
                ToolStatus.REJECTED,
                started,
                error=str(error),
            )

        caller_cancel = cancel_event
        if caller_cancel is not None and caller_cancel.is_set():
            return self._finish(
                invocation_id,
                name,
                ToolStatus.CANCELLED,
                started,
                error="tool invocation was cancelled before execution",
            )

        timeout = definition.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not math.isfinite(timeout) or not 0 < timeout <= definition.timeout_seconds:
            return self._finish(
                invocation_id,
                name,
                ToolStatus.REJECTED,
                started,
                error=f"timeout must be between 0 and {definition.timeout_seconds:g} seconds",
            )

        internal_cancel = threading.Event()
        context = ToolContext(
            invocation_id=invocation_id,
            deadline=started + timeout,
            cancel_event=internal_cancel,
            caller_cancel_event=caller_cancel,
        )
        outcomes: queue.Queue[tuple[ToolStatus, Mapping[str, object] | None, str | None]] = (
            queue.Queue(maxsize=1)
        )

        def execute() -> None:
            try:
                raw_output = definition.handler(arguments, context)
                if not isinstance(raw_output, Mapping):
                    raise TypeError("tool handler must return an object")
                output = json.loads(
                    json.dumps(raw_output, ensure_ascii=False, allow_nan=False)
                )
                context.raise_if_cancelled()
                outcomes.put_nowait((ToolStatus.SUCCESS, output, None))
            except ToolCancelled as error:
                outcomes.put_nowait((ToolStatus.CANCELLED, None, str(error)))
            except ToolDeadlineExceeded as error:
                outcomes.put_nowait((ToolStatus.TIMEOUT, None, str(error)))
            except ToolRejected as error:
                outcomes.put_nowait((ToolStatus.REJECTED, None, str(error)))
            except Exception as error:  # Tool errors are returned, not service-fatal.
                outcomes.put_nowait((ToolStatus.ERROR, None, f"{type(error).__name__}: {error}"))

        worker = threading.Thread(
            target=execute,
            name=f"miso-tool-{name}-{invocation_id[:8]}",
            daemon=True,
        )
        worker.start()
        while True:
            remaining = context.remaining_seconds()
            if caller_cancel is not None and caller_cancel.is_set():
                internal_cancel.set()
                return self._finish(
                    invocation_id,
                    name,
                    ToolStatus.CANCELLED,
                    started,
                    error="tool invocation was cancelled",
                )
            if remaining <= 0:
                internal_cancel.set()
                return self._finish(
                    invocation_id,
                    name,
                    ToolStatus.TIMEOUT,
                    started,
                    error="tool invocation deadline exceeded",
                )
            try:
                status, output, error = outcomes.get(timeout=min(remaining, 0.025))
                return self._finish(
                    invocation_id,
                    name,
                    status,
                    started,
                    output=output,
                    error=error,
                )
            except queue.Empty:
                continue

    def _finish(
        self,
        invocation_id: str,
        name: str,
        status: ToolStatus,
        started: float,
        *,
        output: Mapping[str, object] | None = None,
        error: str | None = None,
    ) -> ToolResult:
        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        result = ToolResult(
            invocation_id=invocation_id,
            tool=name,
            status=status,
            output=output,
            error=error,
            duration_ms=duration_ms,
        )
        self.audit_sink.record(
            audit_event(
                "tool_invocation_finished",
                invocation_id=invocation_id,
                tool=name,
                status=status.value,
                duration_ms=duration_ms,
                error=error,
            )
        )
        return result
