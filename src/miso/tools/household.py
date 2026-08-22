"""Durable timer, reminder, and shared shopping-list tools."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miso.memory import MemoryStore
from miso.tools.audit import AuditSink, audit_event
from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry, ToolRejected

LOGGER = logging.getLogger("miso.tools.household")


def _system_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ToolRejected("due_at must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_due_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ToolRejected("due_at must be an ISO 8601 string")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise ToolRejected("due_at must be a valid ISO 8601 timestamp") from error


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


class HouseholdStore:
    """Transactional household state backed by the Miso SQLite database."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] = _system_now,
    ) -> None:
        self.path = path
        self._now = now

    def migrate(self) -> None:
        MemoryStore(self.path).migrate()

    def connect(self) -> sqlite3.Connection:
        return MemoryStore(self.path).connect()

    def create_scheduled(
        self, kind: str, title: str, due_at: datetime
    ) -> dict[str, object]:
        now = _utc(self._now())
        due = _utc(due_at)
        if due <= now:
            raise ToolRejected("due time must be in the future")
        identifier = str(uuid.uuid4())
        timestamp = _timestamp(now)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_items(
                    id, kind, title, due_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (identifier, kind, title.strip(), _timestamp(due), timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM scheduled_items WHERE id = ?", (identifier,)
            ).fetchone()
        return self._scheduled_dict(row)

    def list_scheduled(self, kind: str, status: str = "pending") -> list[dict[str, object]]:
        self.recover_due()
        statement = "SELECT * FROM scheduled_items WHERE kind = ?"
        values: list[object] = [kind]
        if status != "all":
            statement += " AND status = ?"
            values.append(status)
        statement += " ORDER BY due_at, created_at, id"
        with self.connect() as connection:
            rows = connection.execute(statement, values).fetchall()
        return [self._scheduled_dict(row) for row in rows]

    def update_scheduled(
        self,
        identifier: str,
        kind: str,
        *,
        title: str | None = None,
        due_at: datetime | None = None,
    ) -> dict[str, object]:
        self.recover_due()
        if title is None and due_at is None:
            raise ToolRejected("at least one field must be updated")
        now = _utc(self._now())
        if due_at is not None and _utc(due_at) <= now:
            raise ToolRejected("due time must be in the future")
        updates = ["updated_at = ?", "revision = revision + 1"]
        values: list[object] = [_timestamp(now)]
        if title is not None:
            updates.append("title = ?")
            values.append(title.strip())
        if due_at is not None:
            updates.append("due_at = ?")
            values.append(_timestamp(due_at))
        values.extend((identifier, kind))
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE scheduled_items SET {', '.join(updates)} "
                "WHERE id = ? AND kind = ? AND status = 'pending'",
                values,
            )
            if cursor.rowcount != 1:
                raise ToolRejected(f"pending {kind} was not found")
            row = connection.execute(
                "SELECT * FROM scheduled_items WHERE id = ?", (identifier,)
            ).fetchone()
        return self._scheduled_dict(row)

    def cancel_scheduled(self, identifier: str, kind: str) -> dict[str, object]:
        self.recover_due()
        now = _timestamp(_utc(self._now()))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_items
                SET status = 'cancelled', updated_at = ?, revision = revision + 1
                WHERE id = ? AND kind = ? AND status = 'pending'
                """,
                (now, identifier, kind),
            )
            if cursor.rowcount != 1:
                raise ToolRejected(f"pending {kind} was not found")
            row = connection.execute(
                "SELECT * FROM scheduled_items WHERE id = ?", (identifier,)
            ).fetchone()
        return self._scheduled_dict(row)

    def recover_due(self) -> list[dict[str, object]]:
        """Atomically complete every overdue item, including after restart."""
        now = _timestamp(_utc(self._now()))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM scheduled_items
                WHERE status = 'pending' AND due_at <= ?
                ORDER BY due_at, created_at, id
                """,
                (now,),
            ).fetchall()
            if rows:
                identifiers = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in identifiers)
                connection.execute(
                    f"""
                    UPDATE scheduled_items
                    SET status = 'completed', completed_at = ?, updated_at = ?,
                        revision = revision + 1
                    WHERE id IN ({placeholders}) AND status = 'pending'
                    """,
                    (now, now, *identifiers),
                )
                rows = connection.execute(
                    f"SELECT * FROM scheduled_items WHERE id IN ({placeholders}) "
                    "ORDER BY due_at, created_at, id",
                    identifiers,
                ).fetchall()
        return [self._scheduled_dict(row) for row in rows]

    def add_shopping_item(
        self,
        list_name: str,
        name: str,
        quantity: int,
        added_by: str | None,
    ) -> dict[str, object]:
        now = _timestamp(_utc(self._now()))
        list_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO shopping_lists(id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (list_id, list_name.strip(), now, now),
            )
            row = connection.execute(
                "SELECT id FROM shopping_lists WHERE name = ? COLLATE NOCASE",
                (list_name.strip(),),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO shopping_items(
                    id, list_id, name, quantity, added_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, row["id"], name.strip(), quantity, added_by, now, now),
            )
            item = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE i.id = ?
                """,
                (item_id,),
            ).fetchone()
        return self._shopping_dict(item)

    def list_shopping_items(
        self,
        list_name: str,
        *,
        include_completed: bool = False,
        include_removed: bool = False,
    ) -> list[dict[str, object]]:
        conditions = ["l.name = ? COLLATE NOCASE"]
        values: list[object] = [list_name.strip()]
        if not include_completed:
            conditions.append("i.completed = 0")
        if not include_removed:
            conditions.append("i.status = 'active'")
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT i.*, l.name AS list_name, l.shared
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE {' AND '.join(conditions)}
                ORDER BY i.completed, i.created_at, i.id
                """,
                values,
            ).fetchall()
        return [self._shopping_dict(row) for row in rows]

    def update_shopping_item(
        self,
        identifier: str,
        *,
        name: str | None = None,
        quantity: int | None = None,
        completed: bool | None = None,
    ) -> dict[str, object]:
        if name is None and quantity is None and completed is None:
            raise ToolRejected("at least one field must be updated")
        updates = ["updated_at = ?", "revision = revision + 1"]
        values: list[object] = [_timestamp(_utc(self._now()))]
        for column, value in (("name", name), ("quantity", quantity)):
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(value.strip() if isinstance(value, str) else value)
        if completed is not None:
            updates.append("completed = ?")
            values.append(int(completed))
        values.append(identifier)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE shopping_items SET {', '.join(updates)} "
                "WHERE id = ? AND status = 'active'",
                values,
            )
            if cursor.rowcount != 1:
                raise ToolRejected("active shopping item was not found")
            row = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE i.id = ?
                """,
                (identifier,),
            ).fetchone()
        return self._shopping_dict(row)

    def remove_shopping_item(self, identifier: str) -> dict[str, object]:
        now = _timestamp(_utc(self._now()))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE shopping_items
                SET status = 'removed', updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'active'
                """,
                (now, identifier),
            )
            if cursor.rowcount != 1:
                raise ToolRejected("active shopping item was not found")
            row = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE i.id = ?
                """,
                (identifier,),
            ).fetchone()
        return self._shopping_dict(row)

    @staticmethod
    def _scheduled_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "due_at": row["due_at"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "revision": row["revision"],
        }

    @staticmethod
    def _shopping_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "list_id": row["list_id"],
            "list_name": row["list_name"],
            "shared": bool(row["shared"]),
            "name": row["name"],
            "quantity": row["quantity"],
            "completed": bool(row["completed"]),
            "status": row["status"],
            "added_by": row["added_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "revision": row["revision"],
        }


class ScheduledItemWorker:
    """Reconcile due timers/reminders continuously and after process restart."""

    def __init__(
        self,
        store: HouseholdStore,
        audit_sink: AuditSink,
        *,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        if not 0.01 <= poll_interval_seconds <= 60:
            raise ValueError("scheduled-item poll interval must be between 0.01 and 60")
        self.store = store
        self.audit_sink = audit_sink
        self.poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="miso-scheduled-items",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for item in self.store.recover_due():
                    self.audit_sink.record(
                        audit_event(
                            "scheduled_item_due",
                            scheduled_item_id=item["id"],
                            kind=item["kind"],
                            title=item["title"],
                            due_at=item["due_at"],
                            revision=item["revision"],
                        )
                    )
            except Exception:
                LOGGER.exception("scheduled-item reconciliation failed")
            self._stop.wait(self.poll_interval_seconds)

def _object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def household_tool_definitions(store: HouseholdStore) -> tuple[ToolDefinition, ...]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 64}
    title = {"type": "string", "minLength": 1, "maxLength": 500}
    due_at = {"type": "string", "minLength": 10, "maxLength": 64}
    status = {
        "type": "string",
        "enum": ["pending", "completed", "cancelled", "all"],
    }
    list_name = {"type": "string", "minLength": 1, "maxLength": 100}

    def timer_create(arguments: Mapping[str, object], context: ToolContext):
        context.raise_if_cancelled()
        due = _utc(store._now()) + timedelta(seconds=int(arguments["duration_seconds"]))
        return {
            "timer": store.create_scheduled(
                "timer", str(arguments.get("title", "Timer")), due
            )
        }

    def scheduled_list(kind: str, arguments: Mapping[str, object]):
        return {f"{kind}s": store.list_scheduled(kind, str(arguments.get("status", "pending")))}

    def scheduled_update(kind: str, arguments: Mapping[str, object]):
        title_value = arguments.get("title")
        due_value = None
        if "duration_seconds" in arguments:
            due_value = _utc(store._now()) + timedelta(seconds=int(arguments["duration_seconds"]))
        if "due_at" in arguments:
            due_value = _parse_due_at(arguments["due_at"])
        return {
            kind: store.update_scheduled(
                str(arguments["id"]),
                kind,
                title=str(title_value) if title_value is not None else None,
                due_at=due_value,
            )
        }

    def scheduled_cancel(kind: str, arguments: Mapping[str, object]):
        return {kind: store.cancel_scheduled(str(arguments["id"]), kind)}

    definitions = (
        ToolDefinition(
            "timer_create", "Create a durable countdown timer",
            _object_schema({
                "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 604800},
                "title": title,
            }, ("duration_seconds",)), timer_create,
        ),
        ToolDefinition(
            "timer_list", "List durable timers",
            _object_schema({"status": status}),
            lambda arguments, _context: scheduled_list("timer", arguments),
        ),
        ToolDefinition(
            "timer_update", "Update a pending timer",
            _object_schema({
                "id": identifier,
                "duration_seconds": {
                    "type": "integer", "minimum": 1, "maximum": 604800
                },
                "title": title,
            }, ("id",)),
            lambda arguments, _context: scheduled_update("timer", arguments),
        ),
        ToolDefinition(
            "timer_cancel", "Cancel a pending timer",
            _object_schema({"id": identifier}, ("id",)),
            lambda arguments, _context: scheduled_cancel("timer", arguments),
        ),
        ToolDefinition(
            "reminder_create", "Create a durable reminder",
            _object_schema({"due_at": due_at, "title": title}, ("due_at", "title")),
            lambda arguments, _context: {
                "reminder": store.create_scheduled(
                    "reminder", str(arguments["title"]), _parse_due_at(arguments["due_at"])
                )
            },
        ),
        ToolDefinition(
            "reminder_list", "List durable reminders",
            _object_schema({"status": status}),
            lambda arguments, _context: scheduled_list("reminder", arguments),
        ),
        ToolDefinition(
            "reminder_update", "Update a pending reminder",
            _object_schema({"id": identifier, "due_at": due_at, "title": title}, ("id",)),
            lambda arguments, _context: scheduled_update("reminder", arguments),
        ),
        ToolDefinition(
            "reminder_cancel", "Cancel a pending reminder",
            _object_schema({"id": identifier}, ("id",)),
            lambda arguments, _context: scheduled_cancel("reminder", arguments),
        ),
        ToolDefinition(
            "shopping_add", "Add an item to a shared shopping list",
            _object_schema({
                "list_name": list_name, "name": title,
                "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
                "added_by": {"type": "string", "minLength": 1, "maxLength": 100},
            }, ("name",)),
            lambda arguments, _context: {
                "item": store.add_shopping_item(
                    str(arguments.get("list_name", "shopping")), str(arguments["name"]),
                    int(arguments.get("quantity", 1)),
                    str(arguments["added_by"]) if "added_by" in arguments else None,
                )
            },
        ),
        ToolDefinition(
            "shopping_list", "List items on a shared shopping list",
            _object_schema({
                "list_name": list_name,
                "include_completed": {"type": "boolean"},
                "include_removed": {"type": "boolean"},
            }),
            lambda arguments, _context: {
                "items": store.list_shopping_items(
                    str(arguments.get("list_name", "shopping")),
                    include_completed=bool(arguments.get("include_completed", False)),
                    include_removed=bool(arguments.get("include_removed", False)),
                )
            },
        ),
        ToolDefinition(
            "shopping_update", "Update a shared shopping-list item",
            _object_schema({
                "id": identifier, "name": title,
                "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
                "completed": {"type": "boolean"},
            }, ("id",)),
            lambda arguments, _context: {
                "item": store.update_shopping_item(
                    str(arguments["id"]),
                    name=str(arguments["name"]) if "name" in arguments else None,
                    quantity=int(arguments["quantity"]) if "quantity" in arguments else None,
                    completed=bool(arguments["completed"]) if "completed" in arguments else None,
                )
            },
        ),
        ToolDefinition(
            "shopping_remove", "Remove an item from a shared shopping list",
            _object_schema({"id": identifier}, ("id",)),
            lambda arguments, _context: {
                "item": store.remove_shopping_item(str(arguments["id"]))
            },
        ),
    )
    return definitions


def register_household_tools(
    registry: ToolRegistry,
    database_path: Path,
    *,
    now: Callable[[], datetime] = _system_now,
) -> HouseholdStore:
    store = HouseholdStore(database_path, now=now)
    store.migrate()
    for definition in household_tool_definitions(store):
        registry.register(definition)
    return store
