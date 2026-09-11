"""Durable timer, reminder, and shared shopping-list tools."""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from miso.identity import Actor, SYSTEM_ACTOR, VOICE_ACTOR, private_owner
from miso.memory import MemoryStore
from miso.tools.audit import AuditSink, audit_event
from miso.tools.base import ToolContext, ToolDefinition, ToolRegistry, ToolRejected

LOGGER = logging.getLogger("miso.tools.household")

# The list a request names nothing more specific than "the shopping list".
# Stored names collate case-insensitively, so this matches a list the
# dashboard created as "shopping" just the same.
DEFAULT_LIST_NAME = "Shopping"


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
        self,
        kind: str,
        title: str,
        due_at: datetime,
        *,
        actor: Actor = VOICE_ACTOR,
        visibility: str | None = None,
    ) -> dict[str, object]:
        now = _utc(self._now())
        due = _utc(due_at)
        if due <= now:
            raise ToolRejected("due time must be in the future")
        identifier = str(uuid.uuid4())
        timestamp = _timestamp(now)
        resolved_visibility = visibility or ("private" if actor.is_web else "shared")
        owner = private_owner(actor, resolved_visibility)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scheduled_items(
                    id, kind, title, due_at, created_at, updated_at,
                    visibility, owner_email, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier, kind, title.strip(), _timestamp(due), timestamp,
                    timestamp, resolved_visibility, owner, actor.actor_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM scheduled_items WHERE id = ?", (identifier,)
            ).fetchone()
        return self._scheduled_dict(row)

    def list_scheduled(
        self,
        kind: str,
        status: str = "pending",
        *,
        actor: Actor = VOICE_ACTOR,
    ) -> list[dict[str, object]]:
        self.recover_due()
        statement = (
            "SELECT * FROM scheduled_items WHERE kind = ? "
            "AND (visibility = 'shared' OR owner_email = ?)"
        )
        values: list[object] = [kind, actor.email]
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
        expected_revision: int | None = None,
        actor: Actor = VOICE_ACTOR,
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
        values.extend((identifier, kind, actor.email))
        revision_clause = ""
        if expected_revision is not None:
            revision_clause = " AND revision = ?"
            values.append(expected_revision)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE scheduled_items SET {', '.join(updates)} "
                "WHERE id = ? AND kind = ? AND status = 'pending' "
                f"AND (visibility = 'shared' OR owner_email = ?){revision_clause}",
                values,
            )
            if cursor.rowcount != 1:
                if expected_revision is not None and connection.execute(
                    "SELECT 1 FROM scheduled_items WHERE id = ? AND kind = ? "
                    "AND status = 'pending' "
                    "AND (visibility = 'shared' OR owner_email = ?)",
                    (identifier, kind, actor.email),
                ).fetchone():
                    raise ToolRejected("revision_conflict")
                raise ToolRejected(f"pending {kind} was not found")
            row = connection.execute(
                "SELECT * FROM scheduled_items WHERE id = ?", (identifier,)
            ).fetchone()
        return self._scheduled_dict(row)

    def cancel_scheduled(
        self,
        identifier: str,
        kind: str,
        *,
        expected_revision: int | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> dict[str, object]:
        self.recover_due()
        now = _timestamp(_utc(self._now()))
        with self.connect() as connection:
            revision_clause = "" if expected_revision is None else " AND revision = ?"
            values: tuple[object, ...] = (now, identifier, kind, actor.email)
            if expected_revision is not None:
                values += (expected_revision,)
            cursor = connection.execute(
                f"""
                UPDATE scheduled_items
                SET status = 'cancelled', updated_at = ?, revision = revision + 1
                WHERE id = ? AND kind = ? AND status = 'pending'
                  AND (visibility = 'shared' OR owner_email = ?)
                  {revision_clause}
                """,
                values,
            )
            if cursor.rowcount != 1:
                if expected_revision is not None and connection.execute(
                    "SELECT 1 FROM scheduled_items WHERE id = ? AND kind = ? "
                    "AND status = 'pending' "
                    "AND (visibility = 'shared' OR owner_email = ?)",
                    (identifier, kind, actor.email),
                ).fetchone():
                    raise ToolRejected("revision_conflict")
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
        *,
        actor: Actor = VOICE_ACTOR,
        shared: bool = True,
    ) -> dict[str, object]:
        now = _timestamp(_utc(self._now()))
        list_id = str(uuid.uuid4())
        item_id = str(uuid.uuid4())
        owner = private_owner(actor, "shared" if shared else "private")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO shopping_lists(
                    id, name, shared, created_at, updated_at, owner_email, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (list_id, list_name.strip(), int(shared), now, now, owner, actor.actor_id),
            )
            row = connection.execute(
                """
                SELECT id, shared FROM shopping_lists
                WHERE name = ? COLLATE NOCASE
                  AND (shared = 1 OR owner_email = ?)
                """,
                (list_name.strip(), actor.email),
            ).fetchone()
            if row is None:
                raise ToolRejected("shopping list is not accessible to this actor")
            if bool(row["shared"]) != shared:
                raise ToolRejected("list name already uses different visibility")
            connection.execute(
                """
                INSERT INTO shopping_items(
                    id, list_id, name, quantity, added_by, created_at, updated_at,
                    actor_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id, row["id"], name.strip(), quantity, actor.actor_id,
                    now, now, actor.actor_id,
                ),
            )
            item = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared,
                       l.owner_email, l.created_by
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
        actor: Actor = VOICE_ACTOR,
    ) -> list[dict[str, object]]:
        conditions = [
            "l.name = ? COLLATE NOCASE",
            "(l.shared = 1 OR l.owner_email = ?)",
        ]
        values: list[object] = [list_name.strip(), actor.email]
        if not include_completed:
            conditions.append("i.completed = 0")
        if not include_removed:
            conditions.append("i.status = 'active'")
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT i.*, l.name AS list_name, l.shared,
                       l.owner_email, l.created_by
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE {' AND '.join(conditions)}
                ORDER BY i.completed, i.created_at, i.id
                """,
                values,
            ).fetchall()
        return [self._shopping_dict(row) for row in rows]

    def list_shopping_lists(
        self,
        *,
        include_completed: bool = True,
        actor: Actor = VOICE_ACTOR,
    ) -> list[dict[str, object]]:
        """Return every accessible list with its active items and attribution."""
        conditions = [
            "i.status = 'active'",
            "(l.shared = 1 OR l.owner_email = ?)",
        ]
        if not include_completed:
            conditions.append("i.completed = 0")
        with self.connect() as connection:
            lists = connection.execute(
                "SELECT * FROM shopping_lists "
                "WHERE shared = 1 OR owner_email = ? "
                "ORDER BY name COLLATE NOCASE, created_at, id",
                (actor.email,),
            ).fetchall()
            rows = connection.execute(
                f"""
                SELECT i.*, l.name AS list_name, l.shared,
                       l.owner_email, l.created_by
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE {' AND '.join(conditions)}
                ORDER BY l.name COLLATE NOCASE, i.completed, i.created_at, i.id
                """,
                (actor.email,),
            ).fetchall()
        items_by_list: dict[str, list[dict[str, object]]] = {
            row["id"]: [] for row in lists
        }
        for row in rows:
            items_by_list[row["list_id"]].append(self._shopping_dict(row))
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "shared": bool(row["shared"]),
                "owner_email": row["owner_email"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "items": items_by_list[row["id"]],
            }
            for row in lists
        ]

    def update_shopping_item(
        self,
        identifier: str,
        *,
        name: str | None = None,
        quantity: int | None = None,
        completed: bool | None = None,
        expected_revision: int | None = None,
        actor: Actor = VOICE_ACTOR,
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
        values.extend((identifier, actor.email))
        revision_clause = ""
        if expected_revision is not None:
            revision_clause = " AND revision = ?"
            values.append(expected_revision)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE shopping_items SET {', '.join(updates)} "
                "WHERE id = ? AND status = 'active' AND EXISTS ("
                "SELECT 1 FROM shopping_lists AS l WHERE l.id = shopping_items.list_id "
                f"AND (l.shared = 1 OR l.owner_email = ?)){revision_clause}",
                values,
            )
            if cursor.rowcount != 1:
                if expected_revision is not None and connection.execute(
                    "SELECT 1 FROM shopping_items AS i "
                    "JOIN shopping_lists AS l ON l.id = i.list_id "
                    "WHERE i.id = ? AND i.status = 'active' "
                    "AND (l.shared = 1 OR l.owner_email = ?)",
                    (identifier, actor.email),
                ).fetchone():
                    raise ToolRejected("revision_conflict")
                raise ToolRejected("active shopping item was not found")
            row = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared,
                       l.owner_email, l.created_by
                FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
                WHERE i.id = ?
                """,
                (identifier,),
            ).fetchone()
        return self._shopping_dict(row)

    def find_shopping_item(
        self,
        name: str,
        *,
        list_name: str | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> dict[str, object] | None:
        """Resolve a spoken item name without guessing between matching rows.

        Voice never carries an item id, so removing "the milk" has to find the
        row itself. An exact name wins over a partial one so "milk" never
        removes "oat milk" while a plain "milk" entry is still on the list.
        """
        wanted = name.strip()
        if not wanted:
            return None
        conditions = ["i.status = 'active'", "i.completed = 0", "(l.shared = 1 OR l.owner_email = ?)"]
        values: list[object] = [actor.email]
        if list_name is not None and list_name.strip():
            conditions.append("l.name = ? COLLATE NOCASE")
            values.append(list_name.strip())
        query = f"""
            SELECT i.*, l.name AS list_name, l.shared,
                   l.owner_email, l.created_by
            FROM shopping_items AS i JOIN shopping_lists AS l ON l.id = i.list_id
            WHERE {' AND '.join(conditions)} AND {{match}}
            ORDER BY i.completed, i.created_at DESC, i.id
            LIMIT 2
        """
        escaped = wanted.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        attempts = (
            ("i.name = ? COLLATE NOCASE", [wanted]),
            ("i.name LIKE ? ESCAPE '\\' COLLATE NOCASE", [f"%{escaped}%"]),
        )
        with self.connect() as connection:
            for match, extra in attempts:
                rows = connection.execute(
                    query.format(match=match), (*values, *extra)
                ).fetchall()
                if len(rows) > 1:
                    raise ToolRejected("ambiguous_item")
                if rows:
                    return self._shopping_dict(rows[0])
        return None

    def remove_shopping_item(
        self,
        identifier: str,
        *,
        expected_revision: int | None = None,
        actor: Actor = VOICE_ACTOR,
    ) -> dict[str, object]:
        now = _timestamp(_utc(self._now()))
        with self.connect() as connection:
            revision_clause = "" if expected_revision is None else " AND revision = ?"
            values: tuple[object, ...] = (now, identifier, actor.email)
            if expected_revision is not None:
                values += (expected_revision,)
            cursor = connection.execute(
                f"""
                UPDATE shopping_items
                SET status = 'removed', updated_at = ?, revision = revision + 1
                WHERE id = ? AND status = 'active'
                  AND EXISTS (
                    SELECT 1 FROM shopping_lists AS l
                    WHERE l.id = shopping_items.list_id
                      AND (l.shared = 1 OR l.owner_email = ?)
                  )
                  {revision_clause}
                """,
                values,
            )
            if cursor.rowcount != 1:
                if expected_revision is not None and connection.execute(
                    "SELECT 1 FROM shopping_items AS i "
                    "JOIN shopping_lists AS l ON l.id = i.list_id "
                    "WHERE i.id = ? AND i.status = 'active' "
                    "AND (l.shared = 1 OR l.owner_email = ?)",
                    (identifier, actor.email),
                ).fetchone():
                    raise ToolRejected("revision_conflict")
                raise ToolRejected("active shopping item was not found")
            row = connection.execute(
                """
                SELECT i.*, l.name AS list_name, l.shared,
                       l.owner_email, l.created_by
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
            "visibility": row["visibility"],
            "owner_email": row["owner_email"],
            "created_by": row["created_by"],
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
            "owner_email": row["owner_email"],
            "created_by": row["created_by"],
            "actor_id": row["actor_id"],
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
                            visibility=item["visibility"],
                            owner_email=item["owner_email"],
                            actor=SYSTEM_ACTOR.actor_id,
                            actor_source=SYSTEM_ACTOR.source,
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
    revision = {"type": "integer", "minimum": 1}
    quantity = {"type": "integer", "minimum": 1, "maximum": 999}
    visibility = {"type": "string", "enum": ["shared", "private"]}

    def timer_create(arguments: Mapping[str, object], context: ToolContext):
        context.raise_if_cancelled()
        due = _utc(store._now()) + timedelta(seconds=int(arguments["duration_seconds"]))
        return {
            "timer": store.create_scheduled(
                "timer", str(arguments.get("title", "Timer")), due,
                actor=context.actor,
                visibility=(
                    str(arguments["visibility"])
                    if "visibility" in arguments else None
                ),
            )
        }

    def scheduled_list(
        kind: str, arguments: Mapping[str, object], context: ToolContext
    ):
        return {
            f"{kind}s": store.list_scheduled(
                kind, str(arguments.get("status", "pending")), actor=context.actor
            )
        }

    def scheduled_update(
        kind: str, arguments: Mapping[str, object], context: ToolContext
    ):
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
                expected_revision=(
                    int(arguments["expected_revision"])
                    if "expected_revision" in arguments else None
                ),
                actor=context.actor,
            )
        }

    def scheduled_cancel(
        kind: str, arguments: Mapping[str, object], context: ToolContext
    ):
        return {
            kind: store.cancel_scheduled(
                str(arguments["id"]), kind,
                expected_revision=(
                    int(arguments["expected_revision"])
                    if "expected_revision" in arguments else None
                ),
                actor=context.actor,
            )
        }

    def timer_control(arguments: Mapping[str, object], context: ToolContext):
        context.raise_if_cancelled()
        action = str(arguments["action"])
        if action == "extend" and "seconds" not in arguments:
            raise ToolRejected("seconds is required to extend a timer")
        timers = store.list_scheduled("timer", actor=context.actor)
        wanted = str(arguments.get("title", "")).strip().casefold()
        if wanted:
            timers = [item for item in timers if str(item["title"]).casefold() == wanted]
        if not timers:
            return {"outcome": "not_found", "action": action}
        if len(timers) != 1:
            return {"outcome": "ambiguous", "action": action,
                    "choices": [item["title"] for item in timers]}
        timer = timers[0]
        context.raise_if_cancelled()
        try:
            if action == "cancel":
                timer = store.cancel_scheduled(str(timer["id"]), "timer",
                    expected_revision=int(timer["revision"]), actor=context.actor)
            elif action == "extend":
                # Extend the existing deadline, not the time of the request.
                due = _parse_due_at(timer["due_at"]) + timedelta(seconds=int(arguments["seconds"]))
                timer = store.update_scheduled(str(timer["id"]), "timer", due_at=due,
                    expected_revision=int(timer["revision"]), actor=context.actor)
        except ToolRejected:
            return {"outcome": "changed", "action": action}
        return {"outcome": "done", "action": action, "timer": timer}

    def shopping_add(arguments: Mapping[str, object], context: ToolContext):
        requested = arguments.get("items")
        if requested is None:
            if "name" not in arguments:
                raise ToolRejected("either name or items is required")
            requested = [
                {"name": arguments["name"], "quantity": arguments.get("quantity", 1)}
            ]
        elif "name" in arguments:
            raise ToolRejected("pass either name or items, not both")
        added = [
            store.add_shopping_item(
                str(arguments.get("list_name", DEFAULT_LIST_NAME)),
                str(entry["name"]),
                int(entry.get("quantity", 1)),
                actor=context.actor,
                shared=bool(arguments.get("shared", True)),
            )
            for entry in requested
        ]
        # "item" stays the first one added so single-item callers, the
        # dashboard and the live-event projection keep the shape they read.
        return {"items": added, "item": added[0]}

    def shopping_remove(arguments: Mapping[str, object], context: ToolContext):
        identifier_value = arguments.get("id")
        if identifier_value is None:
            spoken_name = arguments.get("name")
            if not isinstance(spoken_name, str) or not spoken_name.strip():
                raise ToolRejected("either id or name is required")
            try:
                found = store.find_shopping_item(
                    spoken_name,
                    list_name=str(arguments.get("list_name", DEFAULT_LIST_NAME)),
                    actor=context.actor,
                )
            except ToolRejected as error:
                if str(error) != "ambiguous_item":
                    raise
                choices = [item["name"] for item in store.list_shopping_items(
                    str(arguments.get("list_name", DEFAULT_LIST_NAME)), actor=context.actor
                ) if spoken_name.casefold() in str(item["name"]).casefold()]
                return {"item": None, "removed": False, "name": spoken_name,
                        "outcome": "ambiguous", "choices": choices}
            # A name nobody put on the list is an answerable question, not a
            # failure: rejecting it would hand the turn to the model, which
            # knows nothing about the list. An id, which only the dashboard
            # sends, must still exist.
            if found is None:
                return {
                    "item": None, "removed": False, "name": spoken_name.strip(),
                }
            identifier_value = found["id"]
            arguments = {**arguments, "expected_revision": found["revision"]}
        return {
            "item": store.remove_shopping_item(
                str(identifier_value),
                expected_revision=(
                    int(arguments["expected_revision"])
                    if "expected_revision" in arguments else None
                ),
                actor=context.actor,
            ),
            "removed": True,
        }

    definitions = (
        ToolDefinition(
            "timer_control", "Read, extend or cancel a timer by title; omit title only for a single active timer",
            _object_schema({
                "action": {"type": "string", "enum": ["remaining", "extend", "cancel"]},
                "title": title,
                "seconds": {"type": "integer", "minimum": 1, "maximum": 604800},
            }, ("action",)), timer_control,
        ),
        ToolDefinition(
            "timer_create", "Create a durable countdown timer",
            _object_schema({
                "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 604800},
                "title": title,
                "visibility": visibility,
            }, ("duration_seconds",)), timer_create,
        ),
        ToolDefinition(
            "timer_list", "List durable timers",
            _object_schema({"status": status}),
            lambda arguments, context: scheduled_list("timer", arguments, context),
        ),
        ToolDefinition(
            "timer_update", "Update a pending timer",
            _object_schema({
                "id": identifier,
                "duration_seconds": {
                    "type": "integer", "minimum": 1, "maximum": 604800
                },
                "title": title,
                "expected_revision": revision,
            }, ("id",)),
            lambda arguments, context: scheduled_update("timer", arguments, context),
        ),
        ToolDefinition(
            "timer_cancel", "Cancel a pending timer",
            _object_schema({"id": identifier, "expected_revision": revision}, ("id",)),
            lambda arguments, context: scheduled_cancel("timer", arguments, context),
        ),
        ToolDefinition(
            "reminder_create", "Create a durable reminder",
            _object_schema({
                "due_at": due_at, "title": title, "visibility": visibility,
            }, ("due_at", "title")),
            lambda arguments, context: {
                "reminder": store.create_scheduled(
                    "reminder", str(arguments["title"]),
                    _parse_due_at(arguments["due_at"]), actor=context.actor,
                    visibility=(
                        str(arguments["visibility"])
                        if "visibility" in arguments else None
                    ),
                )
            },
        ),
        ToolDefinition(
            "reminder_list", "List durable reminders",
            _object_schema({"status": status}),
            lambda arguments, context: scheduled_list("reminder", arguments, context),
        ),
        ToolDefinition(
            "reminder_update", "Update a pending reminder",
            _object_schema({
                "id": identifier, "due_at": due_at, "title": title,
                "expected_revision": revision,
            }, ("id",)),
            lambda arguments, context: scheduled_update("reminder", arguments, context),
        ),
        ToolDefinition(
            "reminder_cancel", "Cancel a pending reminder",
            _object_schema({"id": identifier, "expected_revision": revision}, ("id",)),
            lambda arguments, context: scheduled_cancel("reminder", arguments, context),
        ),
        ToolDefinition(
            "shopping_add", "Add an item to a shared shopping list",
            _object_schema({
                "list_name": list_name, "name": title,
                "quantity": quantity,
                "shared": {"type": "boolean"},
            }, ("name",)),
            shopping_add,
        ),
        ToolDefinition(
            "shopping_add_many",
            "Add several items to a shared shopping list at once",
            _object_schema({
                "list_name": list_name,
                "items": {
                    "type": "array", "minItems": 1, "maxItems": 20,
                    "items": _object_schema(
                        {"name": title, "quantity": quantity}, ("name",)
                    ),
                },
                "shared": {"type": "boolean"},
            }, ("items",)),
            shopping_add,
        ),
        ToolDefinition(
            "shopping_list", "List items on a shared shopping list",
            _object_schema({
                "list_name": list_name,
                "include_completed": {"type": "boolean"},
                "include_removed": {"type": "boolean"},
            }),
            lambda arguments, context: {
                "items": store.list_shopping_items(
                    str(arguments.get("list_name", DEFAULT_LIST_NAME)),
                    include_completed=bool(arguments.get("include_completed", False)),
                    include_removed=bool(arguments.get("include_removed", False)),
                    actor=context.actor,
                )
            },
        ),
        ToolDefinition(
            "shopping_update", "Update a shared shopping-list item",
            _object_schema({
                "id": identifier, "name": title,
                "quantity": {"type": "integer", "minimum": 1, "maximum": 999},
                "completed": {"type": "boolean"},
                "expected_revision": revision,
            }, ("id",)),
            lambda arguments, context: {
                "item": store.update_shopping_item(
                    str(arguments["id"]),
                    name=str(arguments["name"]) if "name" in arguments else None,
                    quantity=int(arguments["quantity"]) if "quantity" in arguments else None,
                    completed=bool(arguments["completed"]) if "completed" in arguments else None,
                    expected_revision=(
                        int(arguments["expected_revision"])
                        if "expected_revision" in arguments else None
                    ),
                    actor=context.actor,
                )
            },
        ),
        ToolDefinition(
            "shopping_remove",
            "Remove an item from a shared shopping list, by id or by item name",
            _object_schema({
                "id": identifier,
                "name": title,
                "list_name": list_name,
                "expected_revision": revision,
            }),
            shopping_remove,
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
