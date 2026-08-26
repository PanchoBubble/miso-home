"""Durable, authorization-aware live events for household clients."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from miso.identity import Actor, SYSTEM_ACTOR, VOICE_ACTOR, can_access, normalize_email
from miso.memory import MemoryStore, utc_now
from miso.tools.audit import AuditSink
from miso.tools.base import ToolResult


LOGGER = logging.getLogger("miso.live-events")
EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_EVENT_BYTES = 16 * 1024
DEFAULT_CAPACITY = 2_000


@dataclass(frozen=True, slots=True)
class LiveEvent:
    event_id: int
    event_type: str
    payload: Mapping[str, object]
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.event_id,
            "type": self.event_type,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }


class LiveEventStore:
    """Persist events and wake connected clients after committed writes."""

    def __init__(self, path: Path, *, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("live event capacity must be positive")
        self.path = path
        self.capacity = capacity
        self._condition = threading.Condition()
        self._generation = 0
        self._closed = False

    def publish(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        actor: Actor = SYSTEM_ACTOR,
        visibility: str | None = None,
        owner_email: str | None = None,
    ) -> LiveEvent:
        if not EVENT_TYPE_PATTERN.fullmatch(event_type):
            raise ValueError("live event type is invalid")
        resolved_visibility = visibility or ("private" if actor.is_web else "shared")
        if resolved_visibility not in {"shared", "private"}:
            raise ValueError("live event visibility is invalid")
        if resolved_visibility == "private":
            owner = normalize_email(owner_email or actor.email or "")
        else:
            owner = None
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
            raise ValueError("live event payload is too large")
        created_at = utc_now()
        with MemoryStore(self.path).connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO live_events(
                    event_type, payload_json, visibility, owner_email,
                    actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    encoded,
                    resolved_visibility,
                    owner,
                    actor.actor_id,
                    created_at,
                ),
            )
            event_id = int(cursor.lastrowid)
            connection.execute(
                "DELETE FROM live_events WHERE id <= ?",
                (max(0, event_id - self.capacity),),
            )
        event = LiveEvent(event_id, event_type, json.loads(encoded), created_at)
        with self._condition:
            self._generation += 1
            self._condition.notify_all()
        return event

    def after(
        self,
        event_id: int,
        *,
        actor: Actor,
        limit: int = 100,
    ) -> list[LiveEvent]:
        if event_id < 0:
            raise ValueError("live event cursor cannot be negative")
        if not 1 <= limit <= 200:
            raise ValueError("live event limit must be between 1 and 200")
        with MemoryStore(self.path).connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, payload_json, visibility, owner_email, created_at
                FROM live_events
                WHERE id > ? AND (visibility = 'shared' OR owner_email = ?)
                ORDER BY id
                LIMIT ?
                """,
                (event_id, actor.email, limit),
            ).fetchall()
        return [self._row_event(row, actor) for row in rows]

    def recent(self, *, actor: Actor, limit: int = 50) -> list[LiveEvent]:
        if not 1 <= limit <= 200:
            raise ValueError("live event limit must be between 1 and 200")
        with MemoryStore(self.path).connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event_type, payload_json, visibility, owner_email, created_at
                FROM live_events
                WHERE visibility = 'shared' OR owner_email = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (actor.email, limit),
            ).fetchall()
        return [self._row_event(row, actor) for row in reversed(rows)]

    def wait_after(
        self,
        event_id: int,
        *,
        actor: Actor,
        timeout: float,
        limit: int = 100,
    ) -> list[LiveEvent]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._condition:
                generation = self._generation
                if self._closed:
                    return []
            events = self.after(event_id, actor=actor, limit=limit)
            if events:
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            with self._condition:
                if self._closed:
                    return []
                if self._generation == generation:
                    self._condition.wait(remaining)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @staticmethod
    def _row_event(row: Mapping[str, object], actor: Actor) -> LiveEvent:
        visibility = str(row["visibility"])
        owner = None if row["owner_email"] is None else str(row["owner_email"])
        if not can_access(actor, visibility, owner):
            raise PermissionError("live event is not accessible to this actor")
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise RuntimeError("live event payload is malformed")
        return LiveEvent(
            int(row["id"]),
            str(row["event_type"]),
            payload,
            str(row["created_at"]),
        )


class LiveAuditSink:
    """Delegate durable audit writes and project selected events to clients."""

    def __init__(self, delegate: AuditSink, events: LiveEventStore) -> None:
        self.delegate = delegate
        self.events = events

    def record(self, event: Mapping[str, object]) -> None:
        self.delegate.record(event)
        if event.get("event") != "scheduled_item_due":
            return
        visibility = str(event.get("visibility", "shared"))
        owner = event.get("owner_email")
        try:
            self.events.publish(
                "scheduled_item_due",
                {
                    "scheduled_item_id": event.get("scheduled_item_id"),
                    "kind": event.get("kind"),
                    "title": event.get("title"),
                    "due_at": event.get("due_at"),
                    "revision": event.get("revision"),
                },
                actor=SYSTEM_ACTOR,
                visibility=visibility,
                owner_email=None if owner is None else str(owner),
            )
        except Exception:
            LOGGER.exception("could not project audit event to live clients")


class LiveToolResultPublisher:
    """Publish safe tool outcomes and household mutations."""

    HOUSEHOLD_MUTATIONS = frozenset(
        {
            "timer_create",
            "timer_update",
            "timer_cancel",
            "reminder_create",
            "reminder_update",
            "reminder_cancel",
            "shopping_add",
            "shopping_update",
            "shopping_remove",
        }
    )

    def __init__(self, events: LiveEventStore) -> None:
        self.events = events

    def __call__(self, result: ToolResult, actor: Actor) -> None:
        self.events.publish(
            "tool_outcome",
            {
                "invocation_id": result.invocation_id,
                "tool": result.tool,
                "status": result.status.value,
                "duration_ms": result.duration_ms,
            },
            actor=actor,
        )
        if not result.ok or result.tool not in self.HOUSEHOLD_MUTATIONS:
            return
        resource = self._resource(result)
        if resource is None:
            return
        if "visibility" in resource:
            visibility = str(resource["visibility"])
        else:
            visibility = "shared" if bool(resource.get("shared", True)) else "private"
        owner = resource.get("owner_email")
        self.events.publish(
            "household_changed",
            {
                "tool": result.tool,
                "resource_id": resource.get("id"),
                "resource_kind": resource.get("kind", "shopping_item"),
            },
            actor=actor,
            visibility=visibility,
            owner_email=None if owner is None else str(owner),
        )

    @staticmethod
    def _resource(result: ToolResult) -> Mapping[str, object] | None:
        if result.output is None:
            return None
        for key in ("timer", "reminder", "item"):
            value = result.output.get(key)
            if isinstance(value, Mapping):
                return value
        return None


TransitionListener = Callable[[object], None]


def conversation_event_publisher(events: LiveEventStore) -> TransitionListener:
    def publish(transition: object) -> None:
        current = getattr(transition, "current")
        previous = getattr(transition, "previous")
        events.publish(
            "assistant_state",
            {
                "state": current.value,
                "previous": previous.value,
                "occurred_at": round(float(getattr(transition, "occurred_at")), 3),
            },
            actor=VOICE_ACTOR,
        )

    return publish
